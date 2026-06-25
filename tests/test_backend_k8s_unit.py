"""Unit tests for the k8s backend DI mapping — NO pod launch (everything monkeypatched).

These pin the wiring that the integration test exercises end-to-end: the log-event
key the runner emits (`data["line"]`), the pod_env the spec carries, and the
fail-fast/NotImplemented contract. They MUST NOT touch a cluster.
"""
import pytest

from resoluto_sandbox.backends.k8s import K8sBackend
from resoluto_sandbox.contracts import Conduit, NodeResult, SpanEvent


class _FakeConduit(Conduit):
    """An injected store so the backend never calls store_from_env()."""

    async def put(self, key, data): ...
    async def get(self, key): return b""
    async def list_prefix(self, prefix): return []


def _patch_substrate(monkeypatch, *, on_event_payload=None, captured=None):
    """Stub drive_node/runtime/staging so run() never launches a pod.

    on_event_payload: a SpanEvent the fake drive_node feeds to on_event.
    captured: dict that receives the spec passed to drive_node.
    """
    import resoluto_sandbox.driver as driver
    import resoluto_sandbox.runtime.k8s as rt
    import resoluto_sandbox.staging as staging

    async def fake_drive_node(runtime, store, spec, *, on_event=None, **kw):
        if captured is not None:
            captured["spec"] = spec
        if on_event is not None and on_event_payload is not None:
            on_event(on_event_payload)
        return NodeResult(status="success", exit_code=0)

    class FakeRuntime:
        def __init__(self, **kw): pass

    async def fake_put_dir(store, prefix, src): return []
    async def fake_fetch_outputs(store, prefix, dest): return []

    monkeypatch.setattr(driver, "drive_node", fake_drive_node)
    monkeypatch.setattr(rt, "K8sSandboxRuntime", FakeRuntime)
    monkeypatch.setattr(staging, "put_dir", fake_put_dir)
    monkeypatch.setattr(staging, "fetch_outputs", fake_fetch_outputs)


def test_log_line_key_is_captured_into_stdout(monkeypatch):
    ev = SpanEvent(run_id="r", span_id="s", kind="log", event="log", ts=1.0, data={"line": "hi"})
    _patch_substrate(monkeypatch, on_event_payload=ev)
    backend = K8sBackend(image="img:dev", conduit=_FakeConduit())
    out = backend.run(["true"])
    assert "hi" in out.stdout
    assert out.exit_code == 0


def test_pod_env_carries_workload_and_no_aws(monkeypatch):
    for key in list(__import__("os").environ):
        if key.startswith("AWS_"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.delenv("RESOLUTO_TRUSTED_LOCAL", raising=False)
    captured: dict = {}
    _patch_substrate(monkeypatch, captured=captured)
    backend = K8sBackend(image="img:dev", conduit=_FakeConduit())
    backend.run(["python", "agent.py"])
    spec = captured["spec"]
    assert "RESOLUTO_WORKLOAD_ARGV" in spec.env
    assert spec.env["RESOLUTO_WORKSPACE_DIR"] == "/workspace"
    assert not any(k.startswith("AWS_") for k in spec.env)


def test_aws_forwarded_only_when_trusted_local(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "minioadmin")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "minioadmin")
    monkeypatch.setenv("RESOLUTO_TRUSTED_LOCAL", "1")
    monkeypatch.delenv("RESOLUTO_STORE_WRITE_TOKEN", raising=False)
    captured: dict = {}
    _patch_substrate(monkeypatch, captured=captured)
    backend = K8sBackend(image="img:dev", conduit=_FakeConduit())
    backend.run(["true"])
    assert captured["spec"].env["AWS_ACCESS_KEY_ID"] == "minioadmin"


def test_aws_without_trusted_local_raises(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "minioadmin")
    monkeypatch.delenv("RESOLUTO_TRUSTED_LOCAL", raising=False)
    monkeypatch.delenv("RESOLUTO_STORE_WRITE_TOKEN", raising=False)
    _patch_substrate(monkeypatch)
    backend = K8sBackend(image="img:dev", conduit=_FakeConduit())
    with pytest.raises(RuntimeError, match="RESOLUTO_STORE_WRITE_TOKEN"):
        backend.run(["true"])


def test_image_none_raises_value_error():
    with pytest.raises(ValueError):
        K8sBackend(image=None).run(["true"])


def test_stdin_and_deps_raise_not_implemented():
    from resoluto_sandbox.deps import Deps
    with pytest.raises(NotImplementedError):
        K8sBackend(image="img:dev").run(["true"], stdin="x")
    with pytest.raises(NotImplementedError):
        K8sBackend(image="img:dev").run(["true"], deps=Deps(kind="image"))


def test_run_result_reason_populated_from_node_result(monkeypatch):
    import resoluto_sandbox.driver as driver
    import resoluto_sandbox.runtime.k8s as rt
    import resoluto_sandbox.staging as staging

    async def fake_drive_node(runtime, store, spec, *, on_event=None, **kw):
        return NodeResult(status="failure", exit_code=1, reason="OOMKilled")

    class FakeRuntime:
        def __init__(self, **kw): pass

    async def fake_put_dir(store, prefix, src): return []
    async def fake_fetch_outputs(store, prefix, dest): return []

    monkeypatch.setattr(driver, "drive_node", fake_drive_node)
    monkeypatch.setattr(rt, "K8sSandboxRuntime", FakeRuntime)
    monkeypatch.setattr(staging, "put_dir", fake_put_dir)
    monkeypatch.setattr(staging, "fetch_outputs", fake_fetch_outputs)

    backend = K8sBackend(image="img:dev", conduit=_FakeConduit())
    result = backend.run(["true"])
    assert result.reason == "OOMKilled"
    assert result.exit_code == 1
