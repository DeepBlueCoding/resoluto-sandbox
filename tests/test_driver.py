"""Driver composition proof — pool + runtime + store-mediated runner end-to-end.

The FakeRuntime's "pod" actually executes the REAL runner against the SAME store,
so the driver tails REAL telemetry. This exercises the whole rendezvous: acquire →
self-report → tail → result → reap, with no connection ever held between the two."""
import asyncio

import pytest

from resoluto_sandbox.contracts import (
    SandboxHandle,
    SandboxLaunchSpec,
    SandboxRuntime,
    SandboxStatus,
)
from resoluto_sandbox.driver import drive_node
from resoluto_sandbox.objectstore import LocalFsObjectStore
from resoluto_sandbox.pool import SandboxPool
from resoluto_sandbox.runner import run_node_in_sandbox


class RunnerBackedRuntime(SandboxRuntime):
    """Each launch runs the real runner in-process against the shared store."""

    def __init__(self, store, *, run_id="r1"):
        self._store = store
        self._run_id = run_id
        self._tasks: dict[str, asyncio.Future] = {}
        self._n = 0
        self.destroyed: list[str] = []

    async def launch(self, spec: SandboxLaunchSpec) -> SandboxHandle:
        self._n += 1
        hid = f"fake/{self._n}"
        self._tasks[hid] = asyncio.ensure_future(run_node_in_sandbox(
            store=self._store, prefix=spec.store_prefix, run_id=self._run_id,
            node_id=spec.labels.get("node_id", "n"), workload_argv=spec.command,
            heartbeat_interval_s=0.01,
        ))
        return SandboxHandle(id=hid, labels=spec.labels)

    async def status(self, handle: SandboxHandle) -> SandboxStatus:
        task = self._tasks[handle.id]
        if not task.done():
            return SandboxStatus(phase="running")
        res = task.result()
        return SandboxStatus(
            phase="succeeded" if res["status"] == "success" else "failed",
            exit_code=res["exit_code"],
        )

    async def destroy(self, handle: SandboxHandle) -> None:
        self.destroyed.append(handle.id)
        task = self._tasks.get(handle.id)
        if task and not task.done():
            task.cancel()

    async def sweep(self, labels):
        return 0

    async def logs(self, handle, *, tail=200):
        return "fake substrate logs"


class DeadRuntime(SandboxRuntime):
    """Launches, ships nothing, stays 'running' forever — silent substrate death."""

    def __init__(self):
        self.destroyed: list[str] = []

    async def launch(self, spec):
        return SandboxHandle(id="dead/1", labels=spec.labels)

    async def status(self, handle):
        return SandboxStatus(phase="running")

    async def destroy(self, handle):
        self.destroyed.append(handle.id)

    async def sweep(self, labels):
        return 0

    async def logs(self, handle, *, tail=200):
        return "OOMKilled: exit 137"


def _spec(prefix, argv):
    return SandboxLaunchSpec(
        image="busybox", command=argv, store_prefix=prefix, labels={"node_id": "compile"},
    )


async def test_drive_node_full_loop(tmp_path):
    store = LocalFsObjectStore(tmp_path)
    runtime = RunnerBackedRuntime(store)
    pool = SandboxPool(runtime, max_concurrent=2)
    seen = []

    result = await drive_node(
        pool, store, _spec("run/r1/nodes/compile", ["sh", "-c", "echo building; echo ok"]),
        on_event=seen.append, poll_interval_s=0.01,
    )

    assert result["status"] == "success"
    assert result["exit_code"] == 0
    assert result["observed_phase"] == "succeeded"
    # The driver tailed REAL telemetry the runner shipped, in tree order.
    lines = [e.data["line"] for e in seen if e.event == "log" and e.kind == "log"]
    assert lines == ["building", "ok"]
    assert any(e.kind == "node" and e.event == "open" for e in seen)
    assert any(e.kind == "node" and e.event == "close" for e in seen)
    # Lease released → sandbox reaped.
    assert runtime.destroyed == ["fake/1"]
    assert pool.live_count == 0


async def test_drive_node_detects_silent_substrate_death(tmp_path):
    store = LocalFsObjectStore(tmp_path)
    runtime = DeadRuntime()
    pool = SandboxPool(runtime, max_concurrent=1)
    t = {"now": 1000.0}

    # First poll sees nothing and stamps last_progress; advancing the clock past
    # dead_after_s makes is_dead() fire on the next iteration.
    def clock():
        t["now"] += 60.0
        return t["now"]

    result = await drive_node(
        pool, store, _spec("run/r1/nodes/hung", ["true"]),
        poll_interval_s=0, dead_after_s=30.0, clock=clock,
    )

    assert result["status"] == "failure"
    assert "substrate dead" in result["reason"]
    assert "OOMKilled" in result["substrate_logs"]
    assert runtime.destroyed == ["dead/1"]  # reaped even on death path
