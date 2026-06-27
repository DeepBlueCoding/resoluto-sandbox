import asyncio
import logging

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


def _spec(prefix="run/x/nodes/n", **kwargs):
    return SandboxLaunchSpec(image="img", store_prefix=prefix, **kwargs)


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


# ── Deployment-wide admission gate ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_admission_gate_blocks_when_at_cap():
    rt = _FakeRuntime()
    calls = [0]

    async def _gate():
        calls[0] += 1
        return 4  # always at cap

    pool = SandboxPool(rt, max_concurrent=4, admission_gate=_gate, acquire_timeout_s=0.12)
    with pytest.raises(RuntimeError, match="acquire timed out"):
        await pool.acquire(_spec())
    assert calls[0] >= 1


@pytest.mark.asyncio
async def test_admission_gate_allows_when_below_cap():
    rt = _FakeRuntime()

    async def _gate():
        return 2  # below cap of 4

    pool = SandboxPool(rt, max_concurrent=4, admission_gate=_gate)
    async with await pool.acquire(_spec()) as lease:
        assert lease.handle.id == "ns/pod-1"
    assert rt.destroyed == ["ns/pod-1"]


@pytest.mark.asyncio
async def test_admission_gate_fifo_ordering():
    rt = _FakeRuntime()
    count = [3]  # start at cap

    async def _gate():
        return count[0]

    pool = SandboxPool(rt, max_concurrent=4, admission_gate=_gate, acquire_timeout_s=2.0)

    # First acquire blocks because count==cap; release a slot after a short wait
    async def _open_slot():
        await asyncio.sleep(0.05)
        count[0] = 2  # below cap

    asyncio.create_task(_open_slot())
    async with await pool.acquire(_spec()) as lease:
        assert lease.handle.id == "ns/pod-1"


@pytest.mark.asyncio
async def test_semaphore_still_used_without_gate():
    rt = _FakeRuntime()
    pool = SandboxPool(rt, max_concurrent=1)
    a = await pool.acquire(_spec())
    assert pool.available == 0
    blocked = asyncio.create_task(pool.acquire(_spec()))
    await asyncio.sleep(0.02)
    assert not blocked.done()
    await a.release()
    b = await asyncio.wait_for(blocked, timeout=1)
    await b.release()


# The runtime-class isolation guard (kata vs runc + RESOLUTO_TRUSTED_LOCAL) is the K8s
# runtime's private concern now (it owns its runtime_class), NOT the platform-independent
# pool. Coverage lives in test_k8s_manifest.py::test_launch_refuses/permits_non_kata.


# ── Deadlock regression: kind-scoped admission gates ─────────────────────────


@pytest.mark.asyncio
async def test_deadlock_regression_gate_not_starved_by_lanes():
    """Gate pool must admit even when lane pool is saturated.

    Demonstrates that a shared unfiltered count (the old bug) would block gate
    admission: with lane_cap=2 and gate_cap=1, two active lane pods make the
    shared count == 2 >= gate_cap, so the gate pool never acquires. With
    kind-scoped gates, lane pods (kind="lane") are invisible to the gate
    counter (kind="gate") and gate admission succeeds immediately.
    """
    rt = _FakeRuntime()

    # Simulate two active lane pods and zero gate pods — the production deadlock
    # scenario: lane pool is at capacity, gate pool should still be free.
    lane_count = 2  # lane pods active — saturates lane_cap
    gate_count = 0  # no gate pods yet

    async def _shared_unfiltered_gate() -> int:
        # OLD BUG: returns total regardless of kind — lane pods pollute gate budget
        return lane_count + gate_count

    async def _lane_gate() -> int:
        return lane_count

    async def _gate_gate() -> int:
        return gate_count

    # Verify the bug: shared count blocks gate pool (lane_count == gate_cap == 2)
    lane_cap = 2
    gate_cap = 2
    buggy_gate_pool = SandboxPool(
        rt,
        max_concurrent=gate_cap,
        admission_gate=_shared_unfiltered_gate,
        acquire_timeout_s=0.05,
    )
    with pytest.raises(RuntimeError, match="acquire timed out"):
        await buggy_gate_pool.acquire(_spec())

    # Verify the fix: kind-scoped gates — gate pool sees gate_count=0, admits immediately
    fixed_lane_pool = SandboxPool(
        rt,
        max_concurrent=lane_cap,
        admission_gate=_lane_gate,
        acquire_timeout_s=0.05,
    )
    fixed_gate_pool = SandboxPool(
        rt,
        max_concurrent=gate_cap,
        admission_gate=_gate_gate,
        acquire_timeout_s=0.05,
    )

    # Lane pool at cap — cannot acquire (lane_count == lane_cap)
    with pytest.raises(RuntimeError, match="acquire timed out"):
        await fixed_lane_pool.acquire(_spec())

    # Gate pool still admits because its kind-scoped count is 0 < gate_cap
    async with await fixed_gate_pool.acquire(_spec()) as lease:
        assert lease.handle is not None
