"""FIFO-ordered, byte-budgeted admission over a SandboxRuntime."""
from __future__ import annotations

import asyncio

from resoluto.sandbox.contracts import (
    SandboxHandle,
    SandboxLaunchSpec,
    SandboxRuntime,
)
from resoluto.sandbox.resource_semaphore import ResourceSemaphore


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


_ADMISSION_POLL_INTERVAL = 3.0


class SandboxPool:
    """Bounded, FIFO-ordered admission over a runtime with an optional cross-replica `admission_gate` and byte budget."""

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
        self._admit = asyncio.Lock()
        self._acquire_timeout_s = acquire_timeout_s
        self._admission_gate = admission_gate
        self._live: set[str] = set()
        self._max = max_concurrent
        self._mem_sem = ResourceSemaphore(mem_budget_bytes) if mem_budget_bytes else None
        self._mem_budget_provider = mem_budget_provider
        self._budget_resolved = mem_budget_bytes is not None or mem_budget_provider is None
        self._budget_lock = asyncio.Lock()
        self._handle_mem: dict[str, int] = {}

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
            "(substrate starvation — distinct from workload liveness)"
        )

    async def acquire(self, spec: SandboxLaunchSpec, *, on_wait=None) -> SandboxLease:
        """Reserve RAM budget and a count slot, then launch; `on_wait(amount, available)` fires once if the caller parks on the budget."""
        await self._resolve_budget()
        spec_mem = spec.resources.memory_bytes if self._mem_sem is not None else 0
        if self._mem_sem is not None:
            await self._mem_sem.acquire(spec_mem, on_wait=on_wait)
        try:
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
            try:
                handle = await self._runtime.launch(spec)
            except BaseException:
                if self._admission_gate is None:
                    self._sem.release()
                raise
        except BaseException:
            if self._mem_sem is not None:
                self._mem_sem.release(spec_mem)
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
            mem = self._handle_mem.pop(handle.id, 0)
            if self._mem_sem is not None and mem:
                self._mem_sem.release(mem)
