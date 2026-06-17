"""RES-290 increment 1 — resource-aware (per-kind memory budget) admission.

Validates the advisor's acceptance checks against a fake runtime (no cluster):
  1. per-kind isolation (RES-287 regression guard): a full lane budget still admits a gate
  2. graph not double-counted: a 12Gi/10Gi-graph pod counts 12Gi, not 22Gi
  3. a spec that fits admits immediately; one that doesn't WAITS then admits on release
  4. no new timer — the wait reuses acquire_timeout_s and fails loud on true starvation
"""
from __future__ import annotations

import asyncio

import pytest

from resoluto_sandbox.contracts import SandboxHandle, SandboxLaunchSpec, SandboxRuntime, SandboxStatus
from resoluto_sandbox.pool import SandboxPool, _parse_mem

GiB = 1024 ** 3


class _FakeRuntime(SandboxRuntime):
    """In-memory runtime: tracks live pods + their kind + memory so the budget gates
    can be wired to real committed-memory the way count_active_memory(kind) would."""

    def __init__(self) -> None:
        self.live: dict[str, tuple[str, int]] = {}  # id -> (kind, mem_bytes)
        self._n = 0

    async def launch(self, spec: SandboxLaunchSpec) -> SandboxHandle:
        self._n += 1
        pid = f"pod-{self._n}"
        kind = spec.labels.get("resoluto.kind", "")
        self.live[pid] = (kind, _parse_mem(spec.memory))
        return SandboxHandle(id=pid, labels=spec.labels)

    async def status(self, handle: SandboxHandle) -> SandboxStatus:
        return SandboxStatus(phase="running")

    async def destroy(self, handle: SandboxHandle) -> None:
        self.live.pop(handle.id, None)

    async def sweep(self, labels: dict) -> int:
        return 0

    def committed_mem(self, kind: str) -> int:
        return sum(m for (k, m) in self.live.values() if k == kind)


def _spec(kind: str, mem: str, graph: str = "16Gi") -> SandboxLaunchSpec:
    return SandboxLaunchSpec(
        image="x", flavor="plain", memory=mem, docker_graph_size=graph,
        store_prefix=f"verify/{kind}", labels={"resoluto.kind": kind},
    )


def _pool(rt: _FakeRuntime, kind: str, budget_gib: int, *, timeout=5.0) -> SandboxPool:
    return SandboxPool(
        rt, max_concurrent=99, acquire_timeout_s=timeout,
        mem_budget_bytes=budget_gib * GiB,
        committed_mem_gate=lambda: _aimm(rt.committed_mem(kind)),
    )


async def _aimm(v):  # tiny async wrapper so the gate is an awaitable
    return v


@pytest.mark.asyncio
async def test_per_kind_isolation_full_lane_budget_still_admits_gate() -> None:
    # RES-287 regression guard: a SHARED budget would block the gate here.
    rt = _FakeRuntime()
    lane_pool = _pool(rt, "lane", 8)   # 8Gi lane budget
    gate_pool = _pool(rt, "gate", 12)  # 12Gi gate budget (independent)
    # Fill the lane budget (2 x 4Gi = 8Gi).
    await lane_pool.acquire(_spec("lane", "4Gi"))
    await lane_pool.acquire(_spec("lane", "4Gi"))
    assert rt.committed_mem("lane") == 8 * GiB
    # The gate pool must STILL admit despite lanes being full (per-kind budgets).
    lease = await asyncio.wait_for(gate_pool.acquire(_spec("gate", "12Gi", graph="10Gi")), timeout=2)
    assert lease.handle.id in rt.live


@pytest.mark.asyncio
async def test_graph_not_double_counted() -> None:
    # A 12Gi pod with a 10Gi tmpfs graph commits 12Gi (pod limit), not 22Gi.
    rt = _FakeRuntime()
    await rt.launch(_spec("gate", "12Gi", graph="10Gi"))
    assert rt.committed_mem("gate") == 12 * GiB  # graph is inside the pod cgroup, not added


@pytest.mark.asyncio
async def test_oversized_spec_waits_then_admits_on_release() -> None:
    # Budget 12Gi. One 8Gi pod live → an 8Gi waiter doesn't fit (8+8>12); it WAITS,
    # and is admitted only after the first releases.
    rt = _FakeRuntime()
    pool = _pool(rt, "lane", 12, timeout=5.0)
    first = await pool.acquire(_spec("lane", "8Gi"))
    admitted = asyncio.Event()

    async def _second():
        await pool.acquire(_spec("lane", "8Gi"))
        admitted.set()

    task = asyncio.create_task(_second())
    await asyncio.sleep(0.2)
    assert not admitted.is_set()       # blocked: 8+8 > 12
    await first.release()              # frees 8Gi
    await asyncio.wait_for(admitted.wait(), timeout=8)  # now fits
    task.cancel()


@pytest.mark.asyncio
async def test_starvation_fails_loud_not_forever() -> None:
    # A spec that fits the budget but never gets room (budget permanently full) raises
    # the substrate-starvation RuntimeError at the timeout — no new timer, no hang.
    rt = _FakeRuntime()
    pool = _pool(rt, "lane", 8, timeout=1.0)
    await pool.acquire(_spec("lane", "8Gi"))  # fills the budget, never released
    with pytest.raises(RuntimeError, match="acquire timed out"):
        await pool.acquire(_spec("lane", "4Gi"))


@pytest.mark.asyncio
async def test_spec_bigger_than_budget_fails_immediately() -> None:
    # A spec larger than the entire budget can never fit → fail loud at once.
    rt = _FakeRuntime()
    pool = _pool(rt, "gate", 8, timeout=30.0)
    with pytest.raises(RuntimeError, match="exceeds the pool memory budget"):
        await pool.acquire(_spec("gate", "12Gi"))
