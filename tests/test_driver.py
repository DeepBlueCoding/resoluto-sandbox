"""Driver composition proof — pool + runtime + store-mediated runner end-to-end.

The FakeRuntime's "pod" actually executes the REAL runner against the SAME store,
so the driver tails REAL telemetry. This exercises the whole rendezvous: acquire →
self-report → tail → result → reap, with no connection ever held between the two."""
import asyncio

import pytest

from resoluto.sandbox.contracts import (
    SandboxHandle,
    SandboxLaunchSpec,
    SandboxRuntime,
    SandboxStatus,
)
from canary_stub import pass_canary
from resoluto.sandbox.driver import drive_node
from resoluto.sandbox.conduit import LocalConduit
from resoluto.sandbox.pool import SandboxPool
from resoluto.sandbox.runner import run_node_in_sandbox


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
            run_canary=pass_canary,
        ))
        return SandboxHandle(id=hid, labels=spec.labels)

    async def status(self, handle: SandboxHandle) -> SandboxStatus:
        task = self._tasks[handle.id]
        if not task.done():
            return SandboxStatus(phase="running")
        res = task.result()
        return SandboxStatus(
            phase="succeeded" if res.status == "success" else "failed",
            exit_code=res.exit_code,
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
    store = LocalConduit(tmp_path)
    runtime = RunnerBackedRuntime(store)
    pool = SandboxPool(runtime, max_concurrent=2)
    seen = []

    result = await drive_node(
        runtime, store, _spec("run/r1/nodes/compile", ["sh", "-c", "echo building; echo ok"]),
        admit=pool, on_event=seen.append, poll_interval_s=0.01,
    )

    assert result.status == "success"
    assert result.exit_code == 0
    assert result.observed_phase == "succeeded"
    # The driver tailed REAL telemetry the runner shipped, in tree order.
    lines = [e.data["line"] for e in seen if e.event == "log" and e.kind == "log"]
    assert "building" in lines and "ok" in lines
    assert any(e.kind == "node" and e.event == "open" for e in seen)
    assert any(e.kind == "node" and e.event == "close" for e in seen)
    # Lease released → sandbox reaped.
    assert runtime.destroyed == ["fake/1"]
    assert pool.live_count == 0


async def test_drive_node_detects_silent_substrate_death(tmp_path):
    store = LocalConduit(tmp_path)
    runtime = DeadRuntime()
    pool = SandboxPool(runtime, max_concurrent=1)
    t = {"now": 1000.0}

    # First poll sees nothing and stamps last_progress; advancing the clock past
    # dead_after_s makes is_dead() fire on the next iteration.
    def clock():
        t["now"] += 60.0
        return t["now"]

    result = await drive_node(
        runtime, store, _spec("run/r1/nodes/hung", ["true"]),
        admit=pool, poll_interval_s=0, dead_after_s=30.0, clock=clock,
    )

    assert result.status == "failure"
    assert "substrate dead" in result.reason
    assert "OOMKilled" in result.substrate_logs
    assert runtime.destroyed == ["dead/1"]  # reaped even on death path


async def test_drive_node_runs_admission_free(tmp_path):
    # DECOUPLING: drive_node works with NO admission (no pool) — the substrate just
    # launches+tails+reaps. This is the shape for an external admitter (Kueue) or none.
    store = LocalConduit(tmp_path)
    runtime = RunnerBackedRuntime(store)
    result = await drive_node(
        runtime, store, _spec("run/r1/nodes/direct", ["sh", "-c", "echo ok"]),
        poll_interval_s=0.01,   # admit omitted → direct launch, no SandboxPool involved
    )
    assert result.status == "success"
    assert runtime.destroyed == ["fake/1"]  # reaped on the admission-free path too


class PendingThenRunningRuntime(SandboxRuntime):
    """Sits Pending for the first N status() calls, then runs and ships nothing —
    proves a pod waiting to schedule/gate is NOT reaped during the Pending window."""

    def __init__(self, pending_polls):
        self._left = pending_polls
        self.destroyed = []

    async def launch(self, spec):
        return SandboxHandle(id="p/1", labels=spec.labels)

    async def status(self, handle):
        if self._left > 0:
            self._left -= 1
            return SandboxStatus(phase="pending")
        return SandboxStatus(phase="running")

    async def destroy(self, handle):
        self.destroyed.append(handle.id)

    async def sweep(self, labels):
        return 0

    async def logs(self, handle, *, tail=200):
        return "logs"


async def test_drive_node_not_reaped_while_pending(tmp_path):
    # The death clock must NOT count Pending/SchedulingGated time. With a clock that jumps
    # 60s/poll and dead_after_s=30, an un-armed window would reap on the very first poll —
    # the arm-on-running fix must keep the pod alive through the Pending polls.
    store = LocalConduit(tmp_path)
    runtime = PendingThenRunningRuntime(pending_polls=5)
    t = {"now": 0.0}

    def clock():
        t["now"] += 60.0
        return t["now"]

    result = await drive_node(
        runtime, store, _spec("run/r1/nodes/pend", ["true"]),
        poll_interval_s=0, dead_after_s=30.0, clock=clock,
    )
    # It reaches running, arms, THEN (still shipping nothing) is correctly reaped as dead —
    # but only AFTER running, never during the 5 Pending polls.
    assert result.status == "failure"
    assert "substrate dead" in result.reason
    assert runtime._left == 0  # all 5 pending polls were survived (not reaped early)


class _UnstartableRuntime(SandboxRuntime):
    """A pod that never runs: status stays Pending with a fatal waiting reason."""

    def __init__(self):
        self.destroyed: list[str] = []

    async def launch(self, spec):
        return SandboxHandle(id="bad/1", labels=spec.labels)

    async def status(self, handle):
        return SandboxStatus(phase="pending", reason="ImagePullBackOff")

    async def destroy(self, handle):
        self.destroyed.append(handle.id)

    async def sweep(self, labels):
        return 0

    async def logs(self, handle, *, tail=200):
        return "never started"


async def test_drive_node_raw_unstartable_fast_fail(tmp_path):
    # drive_node_raw fails fast (debounced) on a fatal waiting reason and reaps the pod,
    # instead of waiting out the death window — the silence watchdog only arms at RUNNING.
    from resoluto.sandbox.driver import drive_node_raw
    store = LocalConduit(tmp_path)
    rt = _UnstartableRuntime()
    outcome = await drive_node_raw(
        rt, store, _spec("run/r1/nodes/bad", ["true"]),
        poll_interval_s=0, unstartable_polls=3,
    )
    assert outcome.disposition == "unstartable"
    assert "ImagePullBackOff" in outcome.reason
    assert rt.destroyed == ["bad/1"]  # reaped, not leaked


async def test_drive_node_raw_completes_on_result_ready_before_terminal(tmp_path):
    # A caller whose work product lands BEFORE the pod reports terminal (the worker's
    # result.json) finishes as soon as result_ready() is true — the pod may still be running.
    from resoluto.sandbox.driver import drive_node_raw
    store = LocalConduit(tmp_path)

    class _NeverTerminalRuntime(SandboxRuntime):
        def __init__(self):
            self.destroyed: list[str] = []
        async def launch(self, spec):
            return SandboxHandle(id="run/1", labels=spec.labels)
        async def status(self, handle):
            return SandboxStatus(phase="running")  # never terminal
        async def destroy(self, handle):
            self.destroyed.append(handle.id)
        async def sweep(self, labels):
            return 0
        async def logs(self, handle, *, tail=200):
            return ""

    rt = _NeverTerminalRuntime()
    polls = {"n": 0}

    async def ready() -> bool:
        polls["n"] += 1
        return polls["n"] >= 2  # work product appears on the 2nd loop pass

    outcome = await drive_node_raw(
        rt, store, _spec("run/r1/nodes/wp", ["true"]),
        result_ready=ready, poll_interval_s=0,
    )
    assert outcome.disposition == "completed"
    assert rt.destroyed == ["run/1"]  # reaped on completion


def test_sandbox_pool_satisfies_admission_protocol():
    from resoluto.sandbox.contracts import Admission
    from resoluto.sandbox.runtime import k8s  # noqa: F401 — ensure import path is clean
    pool = SandboxPool(DeadRuntime(), max_concurrent=1)
    assert isinstance(pool, Admission)  # structural: pool is a valid admitter, no inheritance


class _CompletedRuntime(SandboxRuntime):
    """Reports terminal 'succeeded' immediately, shipping nothing — the work product is whatever
    is already in the store at result_key (the test seeds it)."""

    def __init__(self):
        self.destroyed: list[str] = []

    async def launch(self, spec):
        return SandboxHandle(id="done/1", labels=spec.labels)

    async def status(self, handle):
        return SandboxStatus(phase="succeeded", exit_code=0)

    async def destroy(self, handle):
        self.destroyed.append(handle.id)

    async def sweep(self, labels):
        return 0

    async def logs(self, handle, *, tail=200):
        return ""


async def test_drive_node_corrupt_result_json_is_attributed_distinctly(tmp_path):
    # A present-but-corrupt result.json must NOT be masked as "no result.json" — the driver
    # distinguishes a parse failure (real serialization bug) from a missing work product.
    from resoluto.sandbox.telemetry import result_key
    store = LocalConduit(tmp_path)
    prefix = "run/r1/nodes/corrupt"
    await store.put(result_key(prefix), b'{"status": 12345}')  # status must be a literal string

    result = await drive_node(
        _CompletedRuntime(), store, _spec(prefix, ["true"]), poll_interval_s=0,
    )

    assert result.status == "failure"
    assert "result.json failed to parse" in result.reason
    assert "no result.json" not in result.reason  # not the missing-work-product path


async def test_drive_node_missing_result_json_is_attributed_as_missing(tmp_path):
    # The complementary branch: completed pod but nothing at result_key → "no result.json".
    store = LocalConduit(tmp_path)
    result = await drive_node(
        _CompletedRuntime(), store, _spec("run/r1/nodes/empty", ["true"]), poll_interval_s=0,
    )
    assert result.status == "failure"
    assert result.reason == "no result.json in store"


class _VanishingRuntime(SandboxRuntime):
    """Runs on the first poll (arming the watchdog), then reports 'unknown' forever — the pod
    was deleted out-of-band. Drives the 'external' disposition."""

    def __init__(self, state):
        self._state = state
        self.destroyed: list[str] = []

    async def launch(self, spec):
        return SandboxHandle(id="ext/1", labels=spec.labels)

    async def status(self, handle):
        self._state["polls"] += 1
        phase = "running" if self._state["polls"] == 1 else "unknown"
        return SandboxStatus(phase=phase)

    async def destroy(self, handle):
        self.destroyed.append(handle.id)

    async def sweep(self, labels):
        return 0

    async def logs(self, handle, *, tail=200):
        return ""


async def test_drive_node_raw_external_disposition_on_vanished_pod(tmp_path):
    # Sustained 'unknown' phase + telemetry silence (after the watchdog armed at running) ==
    # the pod was terminated externally. The worker keys on this disposition.
    from resoluto.sandbox.driver import drive_node_raw
    store = LocalConduit(tmp_path)
    state = {"polls": 0}
    # Time stays 0 (so arm stamps at 0 and the running poll is never "dead") until the pod has
    # been polled as 'unknown', at which point the death window is crossed.
    def clock():
        return 0.0 if state["polls"] <= 1 else 1000.0

    rt = _VanishingRuntime(state)
    outcome = await drive_node_raw(
        rt, store, _spec("run/r1/nodes/vanish", ["true"]),
        poll_interval_s=0, external_gone_polls=1, dead_after_s=30.0, clock=clock,
    )

    assert outcome.disposition == "external"
    assert "terminated externally" in outcome.reason
    assert rt.destroyed == ["ext/1"]  # reaped even when it vanished
