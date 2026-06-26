"""Unit tests for DockerSandboxRuntime — the docker CLI is stubbed (no real container)."""
import pytest

from resoluto_sandbox.contracts import SandboxHandle, SandboxLaunchSpec
from resoluto_sandbox.runtime.docker import DockerSandboxRuntime


def _spec(**kw) -> SandboxLaunchSpec:
    base = dict(
        image="img:dev",
        env={"RESOLUTO_STORE_KIND": "localfs", "K": "V"},
        args=["python", "-m", "resoluto_sandbox.runner_main"],
        labels={"resoluto.run_id": "r1", "resoluto.node_id": "n1"},
        store_prefix="run/r1/nodes/n1/lane-0",
    )
    base.update(kw)
    return SandboxLaunchSpec(**base)


def _stub_docker(monkeypatch, *, returns):
    """Stub the _docker wrapper; record every argv. ``returns`` maps the first docker
    subcommand to a (rc, stdout, stderr) tuple."""
    calls: list[list[str]] = []

    async def fake_docker(*args):
        calls.append(list(args))
        return returns.get(args[0], (0, "", ""))

    import resoluto_sandbox.runtime.docker as mod
    monkeypatch.setattr(mod, "_docker", fake_docker)
    return calls


@pytest.mark.asyncio
async def test_launch_builds_docker_run_argv(monkeypatch):
    calls = _stub_docker(monkeypatch, returns={"run": (0, "abc123\n", "")})
    rt = DockerSandboxRuntime(conduit_host_dir="/host/store", conduit_mount="/conduit")
    handle = await rt.launch(_spec())
    assert handle.id == "abc123"
    assert handle.labels == {"resoluto.run_id": "r1", "resoluto.node_id": "n1"}
    argv = calls[0]
    assert argv[0] == "run" and "-d" in argv
    assert "--label" in argv and "resoluto.run_id=r1" in argv
    assert "-e" in argv and "RESOLUTO_STORE_KIND=localfs" in argv and "K=V" in argv
    i = argv.index("-v")
    assert argv[i + 1] == "/host/store:/conduit"
    img_idx = argv.index("img:dev")
    assert argv[img_idx + 1:] == ["python", "-m", "resoluto_sandbox.runner_main"]
    assert "--privileged" not in argv


@pytest.mark.asyncio
async def test_launch_passes_network_and_privileged(monkeypatch):
    calls = _stub_docker(monkeypatch, returns={"run": (0, "id\n", "")})
    rt = DockerSandboxRuntime(conduit_host_dir="/d", network="resoluto-net")
    await rt.launch(_spec(privileged=True))
    argv = calls[0]
    assert "--network" in argv and argv[argv.index("--network") + 1] == "resoluto-net"
    assert "--privileged" in argv


@pytest.mark.asyncio
async def test_launch_failure_raises(monkeypatch):
    _stub_docker(monkeypatch, returns={"run": (125, "", "no such image")})
    rt = DockerSandboxRuntime(conduit_host_dir="/d")
    with pytest.raises(RuntimeError, match="docker run failed"):
        await rt.launch(_spec())


@pytest.mark.asyncio
@pytest.mark.parametrize("inspect_out,expected_phase,expected_code", [
    ("created|0", "pending", None),
    ("running|0", "running", None),
    ("paused|0", "running", None),
    ("restarting|0", "running", None),
    ("exited|0", "succeeded", 0),
    ("exited|3", "failed", 3),
    ("dead|0", "failed", None),
])
async def test_status_maps_inspect(monkeypatch, inspect_out, expected_phase, expected_code):
    _stub_docker(monkeypatch, returns={"inspect": (0, inspect_out + "\n", "")})
    rt = DockerSandboxRuntime(conduit_host_dir="/d")
    st = await rt.status(SandboxHandle(id="cid"))
    assert st.phase == expected_phase
    assert st.exit_code == expected_code


@pytest.mark.asyncio
async def test_status_missing_container_is_unknown(monkeypatch):
    _stub_docker(monkeypatch, returns={"inspect": (1, "", "No such object")})
    rt = DockerSandboxRuntime(conduit_host_dir="/d")
    st = await rt.status(SandboxHandle(id="gone"))
    assert st.phase == "unknown"


@pytest.mark.asyncio
async def test_destroy_calls_rm_force(monkeypatch):
    calls = _stub_docker(monkeypatch, returns={"rm": (0, "", "")})
    rt = DockerSandboxRuntime(conduit_host_dir="/d")
    await rt.destroy(SandboxHandle(id="cid"))
    assert calls[0] == ["rm", "-f", "cid"]


@pytest.mark.asyncio
async def test_sweep_removes_matching(monkeypatch):
    calls = _stub_docker(monkeypatch, returns={"ps": (0, "c1\nc2\n", ""), "rm": (0, "", "")})
    rt = DockerSandboxRuntime(conduit_host_dir="/d")
    n = await rt.sweep({"resoluto.run_id": "r1"})
    assert n == 2
    ps = calls[0]
    assert ps[0] == "ps" and "label=resoluto.run_id=r1" in ps
    assert calls[1] == ["rm", "-f", "c1"]
    assert calls[2] == ["rm", "-f", "c2"]


@pytest.mark.asyncio
async def test_logs_returns_stdout_and_stderr(monkeypatch):
    _stub_docker(monkeypatch, returns={"logs": (0, "out\n", "err\n")})
    rt = DockerSandboxRuntime(conduit_host_dir="/d")
    text = await rt.logs(SandboxHandle(id="cid"), tail=50)
    assert "out" in text and "err" in text
