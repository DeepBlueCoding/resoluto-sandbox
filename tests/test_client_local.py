"""Local-backend (Docker) `Sandbox.run` wiring — the docker runtime + drive_node
are stubbed so NO real container launches. Asserts the SubstrateBackend the Docker
preset builds: the spec env (RESOLUTO_WORKLOAD_ARGV), the localfs/conduit store_env,
and the NodeResult→RunResult mapping."""
import pytest

from resoluto_sandbox import RunResult, Sandbox, SubstrateBackend
from resoluto_sandbox.contracts import NodeResult, SpanEvent


def _patch_docker_substrate(monkeypatch, *, on_event_payload=None, captured=None, node_result=None):
    """Stub DockerSandboxRuntime + drive_node + staging so run() never touches docker."""
    import resoluto_sandbox.driver as driver
    import resoluto_sandbox.runtime.docker as rt
    import resoluto_sandbox.staging as staging

    async def fake_drive_node(runtime, store, spec, *, on_event=None, **kw):
        if captured is not None:
            captured["spec"] = spec
            captured["runtime"] = runtime
        if on_event is not None and on_event_payload is not None:
            on_event(on_event_payload)
        return node_result or NodeResult(status="success", exit_code=0)

    class FakeRuntime:
        def __init__(self, **kw):
            self.kw = kw

    async def fake_put_dir(store, prefix, src): return []
    async def fake_fetch_outputs(store, prefix, dest): return []

    monkeypatch.setattr(driver, "drive_node", fake_drive_node)
    monkeypatch.setattr(rt, "DockerSandboxRuntime", FakeRuntime)
    monkeypatch.setattr(staging, "put_dir", fake_put_dir)
    monkeypatch.setattr(staging, "fetch_outputs", fake_fetch_outputs)


def test_local_preset_builds_substrate_backend(monkeypatch):
    _patch_docker_substrate(monkeypatch)
    sb = Sandbox(backend="docker")
    assert isinstance(sb._backend, SubstrateBackend)


def test_local_spec_carries_workload_and_localfs_store_env(monkeypatch):
    captured: dict = {}
    _patch_docker_substrate(monkeypatch, captured=captured)
    Sandbox(backend="docker").run(["python", "agent.py"])
    spec = captured["spec"]
    assert spec.env["RESOLUTO_WORKLOAD_ARGV"] == '["python", "agent.py"]'
    assert spec.env["RESOLUTO_STORE_KIND"] == "localfs"
    assert spec.env["RESOLUTO_STORE_ROOT"] == "/conduit"
    assert spec.env["RESOLUTO_TRUSTED_LOCAL"] == "1"
    assert spec.env["RESOLUTO_WORKSPACE_DIR"] == "/workspace"
    assert spec.args == ["python", "-m", "resoluto_sandbox.runner_main"]


def test_local_default_image_and_override(monkeypatch):
    _patch_docker_substrate(monkeypatch)
    from resoluto_sandbox.client import DEFAULT_DOCKER_IMAGE
    assert Sandbox(backend="docker")._backend._image == DEFAULT_DOCKER_IMAGE
    assert Sandbox(backend="docker", image="my:img")._backend._image == "my:img"


def test_local_log_event_streams_into_output(monkeypatch):
    ev = SpanEvent(run_id="r", span_id="s", kind="log", event="log", ts=1.0, data={"line": "hi"})
    _patch_docker_substrate(monkeypatch, on_event_payload=ev)
    out = Sandbox(backend="docker").run(["true"])
    assert "hi" in out.output
    assert out.exit_code == 0


def test_local_maps_node_result_reason_and_exit(monkeypatch):
    nr = NodeResult(status="failure", exit_code=1, reason="boom")
    _patch_docker_substrate(monkeypatch, node_result=nr)
    out = Sandbox(backend="docker").run(["true"])
    assert isinstance(out, RunResult)
    assert out.exit_code == 1
    assert out.reason == "boom"
    assert out.errors == ""


def test_stdin_raises_not_implemented(monkeypatch):
    _patch_docker_substrate(monkeypatch)
    with pytest.raises(NotImplementedError):
        Sandbox(backend="docker").run(["true"], stdin="x")
