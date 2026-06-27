"""Unit tests for the SubstrateBackend + the k8s preset DI mapping — NO pod launch.

These pin the wiring the integration test exercises end-to-end: the log-event key
the runner emits (`data["line"]`), the pod_env the spec carries, the store-env
selection (`store_env_for_pod`), and the fail-fast/NotImplemented contract. They
MUST NOT touch a cluster.
"""
import pytest

from resoluto_sandbox.backends.substrate import SubstrateBackend, store_env_for_pod
from resoluto_sandbox.contracts import (
    Conduit,
    NodeResult,
    SandboxHandle,
    SandboxLaunchSpec,
    SandboxRuntime,
    SandboxStatus,
    SpanEvent,
)


class _FakeConduit(Conduit):
    """An injected store so the backend never reconstructs one."""

    async def put(self, key, data): ...
    async def get(self, key): return b""
    async def list_prefix(self, prefix): return []


class _FakeRuntime(SandboxRuntime):
    async def launch(self, spec): return SandboxHandle(id="x")
    async def status(self, handle): return SandboxStatus(phase="succeeded", exit_code=0)
    async def destroy(self, handle): ...
    async def sweep(self, labels): return 0


def _patch_drive(monkeypatch, *, on_event_payload=None, captured=None, node_result=None):
    """Stub drive_node + staging so run() never launches anything."""
    import resoluto_sandbox.driver as driver
    import resoluto_sandbox.staging as staging

    async def fake_drive_node(runtime, store, spec, *, on_event=None, **kw):
        if captured is not None:
            captured["spec"] = spec
        if on_event is not None and on_event_payload is not None:
            on_event(on_event_payload)
        return node_result or NodeResult(status="success", exit_code=0)

    async def fake_put_dir(store, prefix, src): return []
    async def fake_fetch_outputs(store, prefix, dest): return []

    monkeypatch.setattr(driver, "drive_node", fake_drive_node)
    monkeypatch.setattr(staging, "put_dir", fake_put_dir)
    monkeypatch.setattr(staging, "fetch_outputs", fake_fetch_outputs)


def _backend(store_env=None) -> SubstrateBackend:
    return SubstrateBackend(
        runtime=_FakeRuntime(),
        conduit=_FakeConduit(),
        image="img:dev",
        store_env=store_env or {"RESOLUTO_STORE_KIND": "s3", "RESOLUTO_STORE_BUCKET": "b"},
    )


def test_log_line_key_is_captured_into_stdout(monkeypatch):
    ev = SpanEvent(run_id="r", span_id="s", kind="log", event="log", ts=1.0, data={"line": "hi"})
    _patch_drive(monkeypatch, on_event_payload=ev)
    out = _backend().run(["true"])
    assert "hi" in out.output
    assert out.exit_code == 0


def test_pod_env_carries_workload_and_store_env(monkeypatch):
    captured: dict = {}
    _patch_drive(monkeypatch, captured=captured)
    _backend(store_env={"RESOLUTO_STORE_KIND": "s3", "RESOLUTO_STORE_BUCKET": "b"}).run(
        ["python", "agent.py"]
    )
    spec: SandboxLaunchSpec = captured["spec"]
    assert spec.env["RESOLUTO_WORKLOAD_ARGV"] == '["python", "agent.py"]'
    assert spec.env["RESOLUTO_WORKSPACE_DIR"] == "/workspace"
    assert spec.env["RESOLUTO_STORE_KIND"] == "s3"
    # runtime_class is the K8s runtime's private config now, not a neutral-spec field.
    assert not any(k.startswith("AWS_") for k in spec.env)


def test_image_empty_raises_value_error():
    with pytest.raises(ValueError):
        SubstrateBackend(runtime=_FakeRuntime(), conduit=_FakeConduit(), image="", store_env={})


def test_stdin_raises_not_implemented():
    with pytest.raises(NotImplementedError):
        _backend().run(["true"], stdin="x")


def test_run_result_reason_populated_from_node_result(monkeypatch):
    _patch_drive(monkeypatch, node_result=NodeResult(status="failure", exit_code=1, reason="OOMKilled"))
    result = _backend().run(["true"])
    assert result.reason == "OOMKilled"
    assert result.exit_code == 1


# ── store-env selection (the k8s preset's pod-env policy) ────────────────────


def test_store_env_no_aws_without_trusted_local(monkeypatch):
    for key in list(__import__("os").environ):
        if key.startswith("AWS_"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.delenv("RESOLUTO_TRUSTED_LOCAL", raising=False)
    env = {"RESOLUTO_STORE_KIND": "s3", "RESOLUTO_STORE_BUCKET": "b"}
    selected = store_env_for_pod(env)
    assert selected == env
    assert not any(k.startswith("AWS_") for k in selected)


def test_store_env_aws_forwarded_only_when_trusted_local():
    env = {
        "RESOLUTO_STORE_KIND": "s3",
        "AWS_ACCESS_KEY_ID": "minioadmin",
        "AWS_SECRET_ACCESS_KEY": "minioadmin",
        "RESOLUTO_TRUSTED_LOCAL": "1",
    }
    selected = store_env_for_pod(env)
    assert selected["AWS_ACCESS_KEY_ID"] == "minioadmin"


def test_store_env_aws_without_trusted_local_raises():
    env = {"RESOLUTO_STORE_KIND": "s3", "AWS_ACCESS_KEY_ID": "minioadmin"}
    with pytest.raises(RuntimeError, match="RESOLUTO_STORE_WRITE_TOKEN"):
        store_env_for_pod(env)
