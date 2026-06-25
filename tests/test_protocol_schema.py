import json, jsonschema
from pathlib import Path
from resoluto_sandbox.contracts import SpanEvent, NodeResult

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
