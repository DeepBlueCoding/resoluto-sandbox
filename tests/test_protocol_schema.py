import json, jsonschema, pytest
from pathlib import Path
from resoluto.sandbox.contracts import SpanEvent, NodeResult

SPEC = Path(__file__).resolve().parents[1] / "spec"

def test_spanevent_validates_against_schema():
    schema = json.loads((SPEC / "event.schema.json").read_text())
    sample = SpanEvent(run_id="r", span_id="s", kind="log", event="log", ts=1.0).model_dump()
    jsonschema.validate(sample, schema)

def test_noderesult_validates_against_schema():
    schema = json.loads((SPEC / "result.schema.json").read_text())
    jsonschema.validate(NodeResult(status="success", exit_code=0).model_dump(), schema)

def test_task_schema_validates_sample():
    schema = json.loads((SPEC / "task.schema.json").read_text())
    jsonschema.validate({"workspace_dir": "/w", "env": {"A": "1"}, "output_paths": ["*.py"]}, schema)

def test_manifest_schema_validates_sample():
    schema = json.loads((SPEC / "manifest.schema.json").read_text())
    jsonschema.validate({"total_chunks": 3}, schema)


def test_event_schema_rejects_missing_required():
    schema = json.loads((SPEC / "event.schema.json").read_text())
    bad = {"span_id": "s", "kind": "log", "event": "log", "ts": 1.0}
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(bad, schema)


def test_event_schema_rejects_wrong_type():
    schema = json.loads((SPEC / "event.schema.json").read_text())
    bad = {"run_id": "r", "span_id": "s", "kind": "log", "event": "log", "ts": "now"}
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(bad, schema)


def test_result_schema_rejects_bad_status_enum():
    schema = json.loads((SPEC / "result.schema.json").read_text())
    bad = {"status": "maybe", "exit_code": 0}
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(bad, schema)


def test_task_schema_rejects_missing_required():
    schema = json.loads((SPEC / "task.schema.json").read_text())
    bad = {"prompt": "do it", "env": {"A": "1"}}
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(bad, schema)


def test_manifest_schema_rejects_wrong_type():
    schema = json.loads((SPEC / "manifest.schema.json").read_text())
    bad = {"total_chunks": "three"}
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(bad, schema)
