import asyncio

import pytest

from resoluto_sandbox import SandboxHandle, SandboxLaunchSpec, SandboxPool, SandboxRuntime, SandboxStatus


class _FakeRuntime(SandboxRuntime):
    def __init__(self) -> None:
        self.launched: list[str] = []
        self.destroyed: list[str] = []
        self._n = 0

    async def launch(self, spec):
        self._n += 1
        h = SandboxHandle(id=f"ns/pod-{self._n}", labels=spec.labels)
        self.launched.append(h.id)
        return h

    async def status(self, handle):
        return SandboxStatus(phase="running")

    async def destroy(self, handle):
        self.destroyed.append(handle.id)

    async def sweep(self, labels):
        return 0


def _spec(prefix="run/x/nodes/n"):
    return SandboxLaunchSpec(image="img", store_prefix=prefix)


@pytest.mark.asyncio
async def test_lease_launches_and_destroys():
    rt = _FakeRuntime()
    pool = SandboxPool(rt, max_concurrent=2)
    async with await pool.acquire(_spec()) as lease:
        assert lease.handle.id == "ns/pod-1"
        assert pool.live_count == 1
    assert rt.destroyed == ["ns/pod-1"]
    assert pool.live_count == 0


@pytest.mark.asyncio
async def test_cap_bounds_concurrency():
    rt = _FakeRuntime()
    pool = SandboxPool(rt, max_concurrent=2)
    a = await pool.acquire(_spec())
    b = await pool.acquire(_spec())
    assert pool.available == 0
    # third acquire blocks until one releases
    third = asyncio.create_task(pool.acquire(_spec()))
    await asyncio.sleep(0.02)
    assert not third.done()
    await a.release()
    c = await asyncio.wait_for(third, timeout=1)
    assert pool.live_count == 2  # b + c
    await b.release()
    await c.release()


@pytest.mark.asyncio
async def test_acquire_timeout_fails_loud():
    rt = _FakeRuntime()
    pool = SandboxPool(rt, max_concurrent=1, acquire_timeout_s=0.05)
    held = await pool.acquire(_spec())
    with pytest.raises(RuntimeError, match="acquire timed out"):
        await pool.acquire(_spec())
    await held.release()


@pytest.mark.asyncio
async def test_launch_failure_releases_slot():
    class _Boom(_FakeRuntime):
        async def launch(self, spec):
            raise RuntimeError("launch boom")

    pool = SandboxPool(_Boom(), max_concurrent=1)
    with pytest.raises(RuntimeError, match="launch boom"):
        await pool.acquire(_spec())
    # slot was released — a subsequent acquire (against a good runtime) wouldn't deadlock
    assert pool.available == 1
