"""Runner self-report proof — real subprocess → real localfs store → readable telemetry."""

from unittest.mock import AsyncMock, patch

import pytest
from canary_stub import pass_canary

from resoluto.sandbox.conduit import LocalConduit
from resoluto.sandbox.contracts import NodeResult
from resoluto.sandbox.runner import run_node_in_sandbox
from resoluto.sandbox.telemetry import ChunkReader, result_key


@pytest.fixture
def store(tmp_path):
    return LocalConduit(tmp_path)


async def test_runner_ships_spans_logs_and_result(store):
    prefix = "run/r1/nodes/compile"
    result = await run_node_in_sandbox(
        store=store,
        prefix=prefix,
        run_id="r1",
        node_id="compile",
        workload_argv=["sh", "-c", "echo hello; echo world"],
        run_canary=pass_canary,
    )

    assert (result.node_id, result.status, result.exit_code) == ("compile", "success", 0)

    stored = NodeResult.model_validate_json(await store.get(result_key(prefix)))
    assert stored.status == "success"

    reader = ChunkReader(store, prefix)
    events = await reader.poll()
    opens = [e for e in events if e.event == "open" and e.kind == "node"]
    closes = [e for e in events if e.event == "close" and e.kind == "node"]
    logs = [e.data["line"] for e in events if e.event == "log" and e.kind == "log"]

    assert len(opens) == 1 and opens[0].name == "compile"
    assert len(closes) == 1 and closes[0].status == "success"
    assert closes[0].parent_span_id == "" and closes[0].span_id == opens[0].span_id
    assert "hello" in logs and "world" in logs


async def test_cleanup_hook_always_runs_even_on_workload_failure(store, tmp_path):
    prefix = "run/r1/nodes/gate"
    marker = tmp_path / "cleaned"
    result = await run_node_in_sandbox(
        store=store,
        prefix=prefix,
        run_id="r1",
        node_id="gate",
        workload_argv=["sh", "-c", "echo working; exit 3"],
        cleanup_argv=["sh", "-c", f"echo pruning; touch {marker}"],
        run_canary=pass_canary,
    )

    # workload verdict is preserved; cleanup ran regardless and is observable
    assert (result.status, result.exit_code) == ("failure", 3)
    assert marker.exists()  # cleanup fired despite the failing workload

    events = await ChunkReader(store, prefix).poll()
    cleanup = [e for e in events if e.kind == "cleanup"]
    assert any(e.event == "open" for e in cleanup) and any(e.event == "close" for e in cleanup)
    assert "pruning" in [e.data.get("line") for e in events if e.event == "log"]


async def test_setup_hook_failure_aborts_node_before_workload(store, tmp_path):
    prefix = "run/r1/nodes/staged"
    ran = tmp_path / "workload_ran"
    result = await run_node_in_sandbox(
        store=store,
        prefix=prefix,
        run_id="r1",
        node_id="staged",
        setup_argv=["sh", "-c", "echo bad-setup; exit 2"],
        workload_argv=["sh", "-c", f"touch {ran}"],
        run_canary=pass_canary,
    )

    assert result.status == "failure"
    assert not ran.exists()  # workload never ran because setup failed


async def test_runner_nonzero_exit_marks_failure_but_still_reports(store):
    prefix = "run/r1/nodes/boom"
    result = await run_node_in_sandbox(
        store=store,
        prefix=prefix,
        run_id="r1",
        node_id="boom",
        workload_argv=["sh", "-c", "echo dying; exit 7"],
        run_canary=pass_canary,
    )

    assert (result.node_id, result.status, result.exit_code) == ("boom", "failure", 7)

    reader = ChunkReader(store, prefix)
    events = await reader.poll()
    close = next(e for e in events if e.event == "close" and e.kind == "node")
    assert close.status == "success"  # span body didn't raise — verdict is in result, not span
    stored = NodeResult.model_validate_json(await store.get(result_key(prefix)))
    assert stored.exit_code == 7


async def test_canary_pass_proceeds_to_workload_and_emits_canary_span(store):
    prefix = "run/r2/nodes/canary-pass"
    with (
        patch("resoluto.sandbox.egress_canary.probe_tcp", new=AsyncMock(return_value=False)),
        patch("resoluto.sandbox.egress_canary.probe_store", new=AsyncMock(return_value=True)),
    ):
        result = await run_node_in_sandbox(
            store=store,
            prefix=prefix,
            run_id="r2",
            node_id="canary-pass",
            workload_argv=["sh", "-c", "echo workload-ran"],
        )

    assert result.status == "success"
    assert result.exit_code == 0

    events = await ChunkReader(store, prefix).poll()
    assert any(e.kind == "egress_canary" for e in events)
    logs = [e.data.get("line", "") for e in events if e.event == "log"]
    assert any("workload-ran" in line for line in logs)


async def test_canary_fail_aborts_workload_and_sets_reason(store, tmp_path):
    prefix = "run/r2/nodes/canary-fail"
    ran = tmp_path / "workload_ran"
    # external probe returns True (reachable) → egress not blocked → canary fails
    with (
        patch("resoluto.sandbox.egress_canary.probe_tcp", new=AsyncMock(return_value=True)),
        patch("resoluto.sandbox.egress_canary.probe_store", new=AsyncMock(return_value=True)),
    ):
        result = await run_node_in_sandbox(
            store=store,
            prefix=prefix,
            run_id="r2",
            node_id="canary-fail",
            workload_argv=["sh", "-c", f"touch {ran}"],
        )

    assert result.status == "failure"
    assert "egress" in result.reason
    assert not ran.exists()  # workload never ran


async def test_injected_canary_runs_and_emits_its_span(store):
    prefix = "run/r2/nodes/canary-inject"
    result = await run_node_in_sandbox(
        store=store,
        prefix=prefix,
        run_id="r2",
        node_id="canary-inject",
        workload_argv=["sh", "-c", "echo ok"],
        run_canary=pass_canary,
    )

    assert result.status == "success"
    # the canary always runs (there is no production bypass); the stub just skips real probes
    events = await ChunkReader(store, prefix).poll()
    assert any(e.kind == "egress_canary" for e in events)
