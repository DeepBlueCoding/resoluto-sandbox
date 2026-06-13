"""SandboxPool — platform-independent admission over a SandboxRuntime.

Owns ordered-async admission (FIFO) + a global concurrency cap. Placement is the
runtime's. "ordered async then parallel": requests enter admission in call order;
once admitted they run concurrently up to the cap. The acquire timeout is a
SUBSTRATE timeout (distinct from the no-timeout-on-agent-work law, §5.2/E4).
"""
from __future__ import annotations

import asyncio

from resoluto_sandbox.contracts import SandboxHandle, SandboxLaunchSpec, SandboxRuntime


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
        # The admission lock serialises waiters so launches start in FIFO order;
        # they then proceed concurrently up to the cap once admitted.
        async with self._admit:
            if self._admission_gate is not None:
                loop = asyncio.get_event_loop()
                deadline = loop.time() + self._acquire_timeout_s
                while True:
                    count = await self._admission_gate()
                    if count < self._max:
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
