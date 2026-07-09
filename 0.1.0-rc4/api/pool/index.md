# Pool

Bounded concurrency over sandbox slots: acquire a lease, run inside it, release it back. `Admission` is the concurrency-admission decision and `Lease` the granted slot the pool hands out.

## resoluto.sandbox.SandboxPool

```python
SandboxPool(
    runtime,
    *,
    max_concurrent,
    acquire_timeout_s=600.0,
    admission_gate=None,
    mem_budget_bytes=None,
    mem_budget_provider=None,
)
```

Bounded, FIFO-ordered admission over a runtime with an optional cross-replica `admission_gate` and byte budget.

Source code in `src/resoluto/sandbox/pool.py`

```python
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
```

### acquire

```python
acquire(spec, *, on_wait=None)
```

Reserve RAM budget and a count slot, then launch; `on_wait(amount, available)` fires once if the caller parks on the budget.

Source code in `src/resoluto/sandbox/pool.py`

```python
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
```

## resoluto.sandbox.SandboxLease

```python
SandboxLease(pool, handle)
```

Async-context handle to an acquired sandbox; closing destroys it.

Source code in `src/resoluto/sandbox/pool.py`

```python
def __init__(self, pool: "SandboxPool", handle: SandboxHandle) -> None:
    self._pool = pool
    self.handle = handle
    self._released = False
```

## resoluto.sandbox.Admission

Bases: `Protocol`

Decides whether/when a launch is allowed, then launches and returns a Lease.

## resoluto.sandbox.Lease

Bases: `Protocol`

An acquired sandbox slot as an async context manager exposing the live handle.
