"""SandboxPool — platform-independent admission over a SandboxRuntime.

Owns ordered-async admission (FIFO) + a global concurrency cap. Placement is the
runtime's. "ordered async then parallel": requests enter admission in call order;
once admitted they run concurrently up to the cap. The acquire timeout is a
SUBSTRATE timeout (distinct from the no-timeout-on-agent-work principle).
"""
from __future__ import annotations

import asyncio

from resoluto_sandbox.contracts import (
    SandboxHandle,
    SandboxLaunchSpec,
    SandboxRuntime,
)
from resoluto_sandbox.resource_semaphore import ResourceSemaphore


class SandboxLease:
    """Async-context handle to an acquired sandbox; closing destroys it."""

    def __init__(self, pool: "SandboxPool", handle: SandboxHandle) -> None:
        self._pool = pool
        self.handle = handle
        self._released = False

    async def __aenter__(self) -> "SandboxLease":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.release()

    async def release(self) -> None:
        if self._released:
            return
        self._released = True
        await self._pool._release(self.handle)


_ADMISSION_POLL_INTERVAL = 3.0  # seconds between k8s API count checks


class SandboxPool:
    """Bounded, FIFO-ordered admission over a runtime.

    Inputs: a `SandboxRuntime`, `max_concurrent` (the global cap — the per-host
    RESOLUTO_LANE_CAP lesson, now enforced here), `acquire_timeout_s` (a
    substrate cap on how long a lane may WAIT for a slot, not on its work), and
    an optional `admission_gate` async callable that returns the current active
    pod count. When set, the gate replaces the in-process semaphore so the cap
    spans all worker replicas (the k8s API is the coordination point).
    """

    def __init__(
        self,
        runtime: SandboxRuntime,
        *,
        max_concurrent: int,
        acquire_timeout_s: float = 600.0,
        admission_gate=None,
        mem_budget_bytes: int | None = None,
        mem_budget_provider=None,
    ) -> None:
        if max_concurrent < 1:
            raise ValueError("max_concurrent must be >= 1")
        self._runtime = runtime
        self._sem = asyncio.Semaphore(max_concurrent)
        self._admit = asyncio.Lock()  # serializes admission → strict FIFO ordering
        self._acquire_timeout_s = acquire_timeout_s
        self._admission_gate = admission_gate
        self._live: set[str] = set()
        self._max = max_concurrent
        # Resource-aware admission via a fair byte-budgeted SEMAPHORE.
        # A spec must reserve its memory from a per-kind ResourceSemaphore BEFORE the pod
        # is launched — so a waiter parks event-driven (no spin, no held thread) and holds
        # NO RAM while queued (the pod isn't launched until granted). PER-KIND budget (lane
        # vs gate each own one) keeps the no-deadlock guarantee; the semaphore is
        # FIFO-head-reserving (no starvation) and grants atomically on release (no race).
        # In-process per worker — cross-replica coordination is the k8s ResourceQuota
        # backstop (deferred per the advisor).
        #
        # The budget is either fixed (mem_budget_bytes) or resolved LAZILY on first
        # acquire from mem_budget_provider (an async callable → bytes) — used for the
        # default node-RAM-derived budget, whose source is an async k8s query the
        # (sync) constructor can't await. A provider returning 0 → memory gate off.
        self._mem_sem = ResourceSemaphore(mem_budget_bytes) if mem_budget_bytes else None
        self._mem_budget_provider = mem_budget_provider
        self._budget_resolved = mem_budget_bytes is not None or mem_budget_provider is None
        self._budget_lock = asyncio.Lock()  # one-time lazy resolution under concurrency
        self._handle_mem: dict[str, int] = {}  # handle.id → reserved bytes (for release)

    async def _resolve_budget(self) -> None:
        if self._budget_resolved:
            return
        async with self._budget_lock:
            if self._budget_resolved:
                return
            budget = await self._mem_budget_provider()
            self._mem_sem = ResourceSemaphore(budget) if budget else None
            self._budget_resolved = True

    @property
    def runtime(self) -> SandboxRuntime:
        return self._runtime

    @property
    def live_count(self) -> int:
        return len(self._live)

    @property
    def available(self) -> int:
        return self._max - len(self._live)

    def _starvation_error(self) -> RuntimeError:
        return RuntimeError(
            f"sandbox pool acquire timed out after {self._acquire_timeout_s}s "
            "(substrate starvation — distinct from agent-work liveness)"
        )

    async def acquire(self, spec: SandboxLaunchSpec, *, on_wait=None) -> SandboxLease:
        """Reserve resources (RAM budget + count), then launch. Fail-loud on count
        timeout / launch failure.

        on_wait(amount, available) fires ONCE if the caller must PARK on the memory
        budget — i.e. the execution is now 'queued for resources'. A parked caller
        holds NO RAM (the pod is not launched until granted) and no spin/thread; it is
        woken event-driven when a release frees enough budget (FIFO, no starvation).
        Cancellation (a stop) cleanly drops it from the queue."""
        # The pool is platform-independent: it admits on the NEUTRAL byte budget only. The
        # k8s-only isolation guard (runtimeClass/Kata) is the K8s runtime's own concern.
        await self._resolve_budget()  # lazily derive the default budget on first acquire
        spec_mem = spec.resources.memory_bytes if self._mem_sem is not None else 0
        # 1. RAM budget — the heavy, event-driven gate. Parks holding nothing.
        if self._mem_sem is not None:
            await self._mem_sem.acquire(spec_mem, on_wait=on_wait)
        try:
            # 2. Count cap — a fast secondary ceiling (kind-scoped). With a RAM
            #    budget this rarely blocks (memory is the binding constraint).
            async with self._admit:
                if self._admission_gate is not None:
                    loop = asyncio.get_event_loop()
                    deadline = loop.time() + self._acquire_timeout_s
                    while (await self._admission_gate()) >= self._max:
                        if loop.time() >= deadline:
                            raise self._starvation_error()
                        await asyncio.sleep(_ADMISSION_POLL_INTERVAL)
                else:
                    try:
                        await asyncio.wait_for(self._sem.acquire(), timeout=self._acquire_timeout_s)
                    except asyncio.TimeoutError as exc:
                        raise self._starvation_error() from exc
            # 3. Launch — only now is RAM actually consumed.
            try:
                handle = await self._runtime.launch(spec)
            except BaseException:
                if self._admission_gate is None:
                    self._sem.release()
                raise
        except BaseException:
            if self._mem_sem is not None:
                self._mem_sem.release(spec_mem)  # give the reserved budget back
            raise
        self._live.add(handle.id)
        self._handle_mem[handle.id] = spec_mem
        return SandboxLease(self, handle)

    async def _release(self, handle: SandboxHandle) -> None:
        try:
            await self._runtime.destroy(handle)
        finally:
            self._live.discard(handle.id)
            if self._admission_gate is None:
                self._sem.release()
            # Return the RAM budget → wakes the next queued waiter (event-driven).
            mem = self._handle_mem.pop(handle.id, 0)
            if self._mem_sem is not None and mem:
                self._mem_sem.release(mem)
