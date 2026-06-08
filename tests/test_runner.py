"""Runner self-report proof — real subprocess → real localfs store → readable telemetry."""
import pytest

from resoluto_sandbox.contracts import NodeResult
from resoluto_sandbox.objectstore import LocalFsObjectStore
from resoluto_sandbox.runner import run_node_in_sandbox
from resoluto_sandbox.telemetry import ChunkReader, result_key


@pytest.fixture
def store(tmp_path):
    return LocalFsObjectStore(tmp_path)


async def test_runner_ships_spans_logs_and_result(store):
    prefix = "run/r1/nodes/compile"
    result = await run_node_in_sandbox(
        store=store,
        prefix=prefix,
        run_id="r1",
        node_id="compile",
        workload_argv=["sh", "-c", "echo hello; echo world"],
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
    assert logs == ["hello", "world"]


async def test_runner_nonzero_exit_marks_failure_but_still_reports(store):
    prefix = "run/r1/nodes/boom"
    result = await run_node_in_sandbox(
        store=store, prefix=prefix, run_id="r1", node_id="boom",
        workload_argv=["sh", "-c", "echo dying; exit 7"],
    )

    assert (result.node_id, result.status, result.exit_code) == ("boom", "failure", 7)

    reader = ChunkReader(store, prefix)
    events = await reader.poll()
    close = next(e for e in events if e.event == "close" and e.kind == "node")
    assert close.status == "success"  # span body didn't raise — verdict is in result, not span
    stored = NodeResult.model_validate_json(await store.get(result_key(prefix)))
    assert stored.exit_code == 7
