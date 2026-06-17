"""SandboxPool — platform-independent admission over a SandboxRuntime.

Owns ordered-async admission (FIFO) + a global concurrency cap. Placement is the
runtime's. "ordered async then parallel": requests enter admission in call order;
once admitted they run concurrently up to the cap. The acquire timeout is a
SUBSTRATE timeout (distinct from the no-timeout-on-agent-work law, §5.2/E4).
"""
from __future__ import annotations

import asyncio

from resoluto_sandbox.contracts import SandboxHandle, SandboxLaunchSpec, SandboxRuntime, check_runtime_class_guard


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

_MEM_FACTORS = {
    "Ki": 1024, "Mi": 1024**2, "Gi": 1024**3, "Ti": 1024**4,
    "K": 1000, "M": 1000**2, "G": 1000**3, "T": 1000**4,
}


def _parse_mem(s: str) -> int:
    """Parse a k8s memory quantity ('4Gi', '512Mi', '536870912') to bytes."""
    s = s.strip()
    for suffix, factor in _MEM_FACTORS.items():
        if s.endswith(suffix):
            return int(s[: -len(suffix)]) * factor
    return int(s)


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
        committed_mem_gate=None,
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
        # Resource-aware admission (RES-290). When mem_budget_bytes + committed_mem_gate
        # are set, a spec is admitted only when committed(kind) + spec.memory fits the
        # budget. The budget is PER-KIND (the gate pool and lane pool each get their own
        # gate counting only their kind) — a SHARED budget would re-introduce the RES-287
        # lane↔gate deadlock. The strict-FIFO _admit lock means the head waiter holds
        # admission while it polls, so freed budget flows to it first (automatic head
        # reservation → no heavy-step starvation); within a kind pods are ~homogeneous in
        # size so this never head-of-lines a pod that would fit. No new timer is added —
        # the wait reuses acquire_timeout_s exactly like the count gate.
        self._mem_budget_bytes = mem_budget_bytes
        self._committed_mem_gate = committed_mem_gate

    @property
    def runtime(self) -> SandboxRuntime:
        return self._runtime

    @property
    def live_count(self) -> int:
        return len(self._live)

    @property
    def available(self) -> int:
        return self._max - len(self._live)

    async def acquire(self, spec: SandboxLaunchSpec) -> SandboxLease:
        """Admit (FIFO, bounded) then launch. Raises on acquire-timeout (substrate
        starvation) or launch failure — fail-loud, no degraded fallback."""
        check_runtime_class_guard(spec.runtime_class)
        # The admission lock serialises waiters so launches start in FIFO order;
        # they then proceed concurrently up to the cap once admitted.
        async with self._admit:
            if self._admission_gate is not None or self._mem_budget_bytes is not None:
                loop = asyncio.get_event_loop()
                deadline = loop.time() + self._acquire_timeout_s
                spec_mem = _parse_mem(spec.memory) if self._mem_budget_bytes is not None else 0
                while True:
                    count_ok = True
                    if self._admission_gate is not None:
                        count_ok = (await self._admission_gate()) < self._max
                    mem_ok = True
                    if self._mem_budget_bytes is not None:
                        committed = await self._committed_mem_gate() if self._committed_mem_gate else 0
                        # A spec larger than the whole budget could never fit — fail loud
                        # rather than wait the full timeout for the impossible.
                        if spec_mem > self._mem_budget_bytes:
                            raise RuntimeError(
                                f"spec memory {spec.memory} exceeds the pool memory budget "
                                f"({self._mem_budget_bytes} bytes) — cannot ever be admitted"
                            )
                        mem_ok = committed + spec_mem <= self._mem_budget_bytes
                    if count_ok and mem_ok:
                        break
                    if loop.time() >= deadline:
                        raise RuntimeError(
                            f"sandbox pool acquire timed out after {self._acquire_timeout_s}s "
                            "(substrate starvation — distinct from agent-work liveness)"
                        )
                    await asyncio.sleep(_ADMISSION_POLL_INTERVAL)
            else:
                try:
                    await asyncio.wait_for(self._sem.acquire(), timeout=self._acquire_timeout_s)
                except asyncio.TimeoutError as exc:
                    raise RuntimeError(
                        f"sandbox pool acquire timed out after {self._acquire_timeout_s}s "
                        "(substrate starvation — distinct from agent-work liveness)"
                    ) from exc
        try:
            handle = await self._runtime.launch(spec)
        except BaseException:
            if self._admission_gate is None:
                self._sem.release()
            raise
        self._live.add(handle.id)
        return SandboxLease(self, handle)

    async def _release(self, handle: SandboxHandle) -> None:
        try:
            await self._runtime.destroy(handle)
        finally:
            self._live.discard(handle.id)
            if self._admission_gate is None:
                self._sem.release()
