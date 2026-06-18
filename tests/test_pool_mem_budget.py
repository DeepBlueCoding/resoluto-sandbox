"""RES-290/291 — SandboxPool wires the per-kind ResourceSemaphore correctly.

A spec that doesn't fit the pool's RAM budget PARKS without launching a pod (no RAM
on hold), is granted event-driven on release, and per-kind pools are independent
(RES-287 no-deadlock). The fair-semaphore internals are proven in
test_resource_semaphore.py; here we prove the POOL's launch/release wiring.
"""
from __future__ import annotations

import asyncio

import pytest

from resoluto_sandbox.contracts import SandboxHandle, SandboxLaunchSpec, SandboxRuntime, SandboxStatus
from resoluto_sandbox.pool import SandboxPool

GiB = 1024 ** 3


class _FakeRuntime(SandboxRuntime):
    def __init__(self) -> None:
        self.launched: list[str] = []   # ids, in launch order
        self.live: set[str] = set()
        self._n = 0

    async def launch(self, spec: SandboxLaunchSpec) -> SandboxHandle:
        self._n += 1
        pid = f"pod-{self._n}"
        self.launched.append(pid)
        self.live.add(pid)
        return SandboxHandle(id=pid, labels=spec.labels)

    async def status(self, handle: SandboxHandle) -> SandboxStatus:
        return SandboxStatus(phase="running")

    async def destroy(self, handle: SandboxHandle) -> None:
        self.live.discard(handle.id)

    async def sweep(self, labels: dict) -> int:
        return 0


def _spec(kind: str, mem: str, graph: str = "16Gi") -> SandboxLaunchSpec:
    return SandboxLaunchSpec(
        image="x", flavor="plain", memory=mem, docker_graph_size=graph,
        store_prefix=f"verify/{kind}", labels={"resoluto.kind": kind},
    )


def _pool(rt: _FakeRuntime, budget_gib: int, *, timeout=5.0) -> SandboxPool:
    return SandboxPool(rt, max_concurrent=99, acquire_timeout_s=timeout,
                       mem_budget_bytes=budget_gib * GiB)


@pytest.mark.asyncio
async def test_oversized_spec_parks_then_launches_on_release() -> None:
    rt = _FakeRuntime()
    pool = _pool(rt, 12)                         # 12Gi budget
    first = await pool.acquire(_spec("lane", "8Gi"))
    assert rt.launched == ["pod-1"]              # launched
    queued: list = []

    async def second():
        await pool.acquire(_spec("lane", "8Gi"), on_wait=lambda a, av: queued.append((a, av)))

    t = asyncio.create_task(second())
    await asyncio.sleep(0.1)
    assert rt.launched == ["pod-1"]              # 2nd did NOT launch — parked, holding no RAM
    assert queued and queued[0][0] == 8 * GiB    # 'queued for resources' signal fired
    await first.release()                        # frees 8Gi → wakes the waiter
    await asyncio.wait_for(asyncio.sleep(0.1), timeout=2)
    assert rt.launched == ["pod-1", "pod-2"]     # now launched
    t.cancel()


@pytest.mark.asyncio
async def test_per_kind_pools_are_independent_no_deadlock() -> None:
    # RES-287 guard: separate pools (lane/gate) own separate semaphores, so a FULL
    # lane budget cannot block a gate acquire.
    rt = _FakeRuntime()
    lane_pool = _pool(rt, 8)
    gate_pool = _pool(rt, 12)
    await lane_pool.acquire(_spec("lane", "4Gi"))
    await lane_pool.acquire(_spec("lane", "4Gi"))   # lane budget now full
    lease = await asyncio.wait_for(gate_pool.acquire(_spec("gate", "12Gi")), timeout=2)
    assert lease.handle.id in rt.live               # gate admitted despite lanes full


@pytest.mark.asyncio
async def test_parked_holds_no_pod_until_granted() -> None:
    # Explicitly: while parked, zero pods beyond what fits are launched.
    rt = _FakeRuntime()
    pool = _pool(rt, 4)
    a = await pool.acquire(_spec("lane", "4Gi"))    # fills budget
    blocked = asyncio.create_task(pool.acquire(_spec("lane", "4Gi")))
    await asyncio.sleep(0.1)
    assert len(rt.live) == 1                         # only the first pod is live
    await a.release()
    await asyncio.sleep(0.1)
    assert len(rt.live) == 1                         # still one live (the waiter took the freed slot)
    blocked.cancel()


@pytest.mark.asyncio
async def test_oversized_beyond_budget_fails_loud() -> None:
    rt = _FakeRuntime()
    pool = _pool(rt, 8, timeout=30)
    with pytest.raises(RuntimeError, match="can never be admitted"):
        await pool.acquire(_spec("gate", "12Gi"))
    assert rt.launched == []                         # never launched


@pytest.mark.asyncio
async def test_no_budget_admits_freely() -> None:
    rt = _FakeRuntime()
    pool = SandboxPool(rt, max_concurrent=99)        # no mem budget → gate off
    for _ in range(3):
        await pool.acquire(_spec("lane", "8Gi"))
    assert len(rt.live) == 3


@pytest.mark.asyncio
async def test_lazy_budget_provider_resolved_once_on_first_acquire() -> None:
    # The DEFAULT path: budget derived from an async provider (node RAM), resolved
    # lazily on first acquire, then enforced like a fixed budget.
    rt = _FakeRuntime()
    calls = []

    async def provider() -> int:
        calls.append(1)
        return 12 * GiB

    pool = SandboxPool(rt, max_concurrent=99, mem_budget_provider=provider)
    a = await pool.acquire(_spec("lane", "8Gi"))     # resolves budget=12Gi, fits
    blocked = asyncio.create_task(pool.acquire(_spec("lane", "8Gi")))  # 8+8>12 → parks
    await asyncio.sleep(0.1)
    assert len(rt.live) == 1                          # provider budget is enforced
    assert len(calls) == 1                            # resolved exactly once
    await a.release()
    await asyncio.sleep(0.1)
    assert len(calls) == 1                            # not re-resolved on later acquires
    blocked.cancel()


@pytest.mark.asyncio
async def test_lazy_provider_zero_means_gate_off() -> None:
    # node RAM unknown (offline/tests) → provider returns 0 → memory gate off.
    rt = _FakeRuntime()
    pool = SandboxPool(rt, max_concurrent=99, mem_budget_provider=lambda: _zero())
    for _ in range(3):
        await pool.acquire(_spec("lane", "99Gi"))    # huge, but gate is off
    assert len(rt.live) == 3


async def _zero() -> int:
    return 0
