"""Local-backend (Kata/nerdctl) `Sandbox.run` wiring — the runtime + drive_node are stubbed so
NO real microVM launches. Asserts the SubstrateBackend the local backend builds: the spec env
(RESOLUTO_WORKLOAD_ARGV), the localfs/conduit store_env (no trusted-local), and the
NodeResult→RunResult mapping."""
import pytest

from resoluto.sandbox import RunResult, Sandbox
from resoluto.sandbox.contracts import NodeResult, SpanEvent


def _patch_local_substrate(monkeypatch, *, on_event_payload=None, captured=None, node_result=None):
    """Stub KataNerdctlSandboxRuntime + drive_node + staging so run() never touches a VM."""
    import resoluto.sandbox.driver as driver
    import resoluto.sandbox.runtime.kata_nerdctl as rt
    import resoluto.sandbox.staging as staging

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

        @classmethod
        def from_env(cls, **kw):
            return cls(**kw)

    async def fake_put_dir(store, prefix, src): return []
    async def fake_fetch_outputs(store, prefix, dest): return []

    monkeypatch.setattr(driver, "drive_node", fake_drive_node)
    monkeypatch.setattr(rt, "KataNerdctlSandboxRuntime", FakeRuntime)
    monkeypatch.setattr(staging, "put_dir", fake_put_dir)
    monkeypatch.setattr(staging, "fetch_outputs", fake_fetch_outputs)


def test_local_spec_carries_workload_and_localfs_store_env(monkeypatch):
    captured: dict = {}
    _patch_local_substrate(monkeypatch, captured=captured)
    Sandbox(backend="local").run(["python", "agent.py"])
    spec = captured["spec"]
    assert spec.env["RESOLUTO_WORKLOAD_ARGV"] == '["python", "agent.py"]'
    assert spec.env["RESOLUTO_STORE_KIND"] == "localfs"
    assert spec.env["RESOLUTO_STORE_ROOT"] == "/conduit"
    # NO trusted-local relaxation — the local backend is VM-isolated, egress enforced in-guest.
    assert "RESOLUTO_TRUSTED_LOCAL" not in spec.env
    assert spec.env["RESOLUTO_WORKSPACE_DIR"] == "/workspace"
    assert spec.args == ["python", "-m", "resoluto.sandbox.runner_main"]


def test_local_default_image_and_override(monkeypatch):
    _patch_local_substrate(monkeypatch)
    from importlib.metadata import version as pkg_version

    from resoluto.sandbox.client import default_local_image
    assert default_local_image() == f"resoluto-sandbox-base:{pkg_version('resoluto-sandbox')}"
    assert Sandbox(backend="local")._backend._image == default_local_image()
    assert Sandbox(backend="local", image="my:img")._backend._image == "my:img"


def test_local_log_event_streams_into_output(monkeypatch):
    ev = SpanEvent(run_id="r", span_id="s", kind="log", event="log", ts=1.0, data={"line": "hi"})
    _patch_local_substrate(monkeypatch, on_event_payload=ev)
    out = Sandbox(backend="local").run(["true"])
    assert "hi" in out.output
    assert out.exit_code == 0


def test_local_maps_node_result_reason_and_exit(monkeypatch):
    nr = NodeResult(status="failure", exit_code=1, reason="boom")
    _patch_local_substrate(monkeypatch, node_result=nr)
    out = Sandbox(backend="local").run(["true"])
    assert isinstance(out, RunResult)
    assert out.exit_code == 1
    assert out.reason == "boom"
    assert out.errors == ""


def test_stdin_raises_not_implemented(monkeypatch):
    _patch_local_substrate(monkeypatch)
    with pytest.raises(NotImplementedError):
        Sandbox(backend="local").run(["true"], stdin="x")


def test_local_conduit_base_is_user_private_0700():
    # The conduit leaf stays world-writable (the Kata guest writes as a different uid), so the
    # parent MUST be 0o700 and user-owned — otherwise another local user could read/tamper lane
    # data. This guards the host→lane bind-mount permission invariant.
    import os
    import stat

    from resoluto.sandbox.client import _local_conduit_base

    base = _local_conduit_base()
    st = os.stat(base)
    assert stat.S_IMODE(st.st_mode) == 0o700, oct(stat.S_IMODE(st.st_mode))
    assert st.st_uid == os.getuid()
