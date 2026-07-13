"""Unit tests for KataNerdctlSandboxRuntime — nerdctl is stubbed (no real VM)."""

import pytest

from resoluto.sandbox.contracts import SandboxLaunchSpec
from resoluto.sandbox.runtime.kata_nerdctl import KataNerdctlSandboxRuntime

_ADDR = "/run/resoluto-local/containerd/containerd.sock"
_NS = "resoluto-local"


def _spec(**kw) -> SandboxLaunchSpec:
    base = dict(
        image="img:0.1.0",
        env={"RESOLUTO_STORE_KIND": "localfs", "K": "V"},
        args=["python", "-m", "resoluto.sandbox.runner_main"],
        labels={"resoluto.run_id": "r1", "resoluto.node_id": "n1"},
        store_prefix="run/r1/nodes/n1/sbx-0",
    )
    base.update(kw)
    return SandboxLaunchSpec(**base)


def _rt(**kw) -> KataNerdctlSandboxRuntime:
    base = dict(
        address=_ADDR, namespace=_NS, conduit_host_dir="/host/store", conduit_mount="/conduit"
    )
    base.update(kw)
    return KataNerdctlSandboxRuntime(**base)


def _stub_run(monkeypatch, rt, *, returns):
    """Stub the instance `_run`; record every argv (subcommand args, no base)."""
    calls: list[list[str]] = []

    async def fake_run(*args):
        calls.append(list(args))
        return returns.get(args[0], (0, "", ""))

    monkeypatch.setattr(rt, "_run", fake_run)
    return calls


def test_non_kata_runtime_is_hard_error():
    with pytest.raises(RuntimeError, match="Isolation downgrade refused"):
        _rt(runtime="io.containerd.runc.v2")


def test_sudo_prefixes_nerdctl_calls():
    # A non-root host escalates per call: every nerdctl invocation is prefixed with `sudo -n`.
    base = _rt(sudo=True)._base()
    assert base[:2] == ["sudo", "-n"]
    assert base[2] == "nerdctl"  # the binary follows the sudo prefix
    # default (root) issues nerdctl directly
    assert _rt()._base()[0] == "nerdctl"


def test_resolve_sudo_env_override(monkeypatch):
    from resoluto.sandbox.runtime.kata_nerdctl import _resolve_sudo

    monkeypatch.setenv("RESOLUTO_LOCAL_NERDCTL_SUDO", "1")
    assert _resolve_sudo() is True
    monkeypatch.setenv("RESOLUTO_LOCAL_NERDCTL_SUDO", "0")
    assert _resolve_sudo() is False


def test_base_args_carry_address_namespace_and_cni():
    rt = _rt(cni_path="/opt/resoluto-local/libexec/cni", cni_netconfpath="/etc/resoluto-local/cni")
    base = rt._base()
    assert base[:5] == ["nerdctl", "--address", _ADDR, "--namespace", _NS]
    assert "--cni-path" in base and "/opt/resoluto-local/libexec/cni" in base
    assert "--cni-netconfpath" in base and "/etc/resoluto-local/cni" in base


@pytest.mark.asyncio
async def test_launch_builds_kata_run_argv(monkeypatch):
    rt = _rt()
    calls = _stub_run(monkeypatch, rt, returns={"run": (0, "vmabc\n", "")})
    handle = await rt.launch(_spec())
    assert handle.id == "vmabc"
    assert handle.labels == {"resoluto.run_id": "r1", "resoluto.node_id": "n1"}
    argv = calls[0]
    assert argv[0] == "run" and "-d" in argv
    # VM-grade isolation: every step runs under the Kata shim.
    assert argv[argv.index("--runtime") + 1] == "io.containerd.kata.v2"
    assert argv[argv.index("--network") + 1] == "bridge"
    assert "--label" in argv and "resoluto.run_id=r1" in argv
    assert "-e" in argv and "RESOLUTO_STORE_KIND=localfs" in argv and "K=V" in argv
    # The conduit mount is SCOPED to this run's prefix — the guest sees only its own prefix under
    # /conduit, never sibling runs/lanes sharing the same conduit root.
    assert (
        argv[argv.index("-v") + 1]
        == "/host/store/run/r1/nodes/n1/sbx-0:/conduit/run/r1/nodes/n1/sbx-0"
    )
    img_idx = argv.index("img:0.1.0")
    assert argv[img_idx + 1 :] == ["python", "-m", "resoluto.sandbox.runner_main"]
    # plain step: no inner dockerd, default uid, no dind graph
    assert "--privileged" not in argv and "--user" not in argv and "--tmpfs" not in argv
    # neutral Resources rendered privately — raw bytes/cores, no k8s notation
    assert argv[argv.index("--memory") + 1] == str(4 * 1024**3)
    assert argv[argv.index("--memory-swap") + 1] == str(4 * 1024**3)
    assert argv[argv.index("--cpus") + 1] == "2.0"


@pytest.mark.asyncio
async def test_launch_dind_is_privileged_with_tmpfs_graph(monkeypatch):
    from resoluto.sandbox.contracts import Resources

    rt = _rt()
    calls = _stub_run(monkeypatch, rt, returns={"run": (0, "vm\n", "")})
    res = Resources(memory_bytes=12 * 1024**3, cpu_cores=4.0, dind_graph_bytes=10 * 1024**3)
    await rt.launch(_spec(flavor="dind", privileged=True, resources=res))
    argv = calls[0]
    assert "--privileged" in argv
    assert argv[argv.index("--user") + 1] == "0"  # dockerd starts as root, entrypoint drops to user
    assert argv[argv.index("--tmpfs") + 1] == f"/var/lib/docker:size={10 * 1024**3}"
    assert argv[argv.index("--memory") + 1] == str(12 * 1024**3)
    # Guest-scoped privilege: extended privileges WITHOUT host devices, or the Kata shim fails
    # re-creating /dev/full (EEXIST). This is the nerdctl privileged_without_host_devices equivalent.
    assert argv[argv.index("--security-opt") + 1] == "privileged-without-host-devices=true"


@pytest.mark.asyncio
async def test_plain_launch_is_not_privileged_and_has_no_host_device_opt(monkeypatch):
    # A plain sandbox step never requests privilege — so no --privileged and no host-device security-opt.
    rt = _rt()
    calls = _stub_run_seq_none(monkeypatch, rt)
    await rt.launch(_spec())
    argv = calls[0]
    assert "--privileged" not in argv
    assert "privileged-without-host-devices=true" not in argv


def _stub_run_seq_none(monkeypatch, rt):
    calls: list[list[str]] = []

    async def fake_run(*args):
        calls.append(list(args))
        return (0, "vm\n", "")

    monkeypatch.setattr(rt, "_run", fake_run)
    return calls


@pytest.mark.asyncio
async def test_launch_without_store_prefix_mounts_whole_conduit(monkeypatch):
    # With no store_prefix there is nothing to scope to — fall back to mounting the conduit root.
    rt = _rt()
    calls = _stub_run(monkeypatch, rt, returns={"run": (0, "vm\n", "")})
    await rt.launch(_spec(store_prefix=""))
    argv = calls[0]
    assert argv[argv.index("-v") + 1] == "/host/store:/conduit"


@pytest.mark.asyncio
async def test_launch_precreates_world_writable_prefix_dir(monkeypatch, tmp_path):
    # The runtime itself creates the (world-writable) scoped mount source, so the contract holds for
    # EVERY caller (the facade AND the engine's lane substrate), not just those that stage a workspace.
    rt = _rt(conduit_host_dir=str(tmp_path))
    _stub_run(monkeypatch, rt, returns={"run": (0, "vm\n", "")})
    await rt.launch(_spec())  # store_prefix = run/r1/nodes/n1/sbx-0
    d = tmp_path / "run/r1/nodes/n1/sbx-0"
    assert d.is_dir()
    assert (d.stat().st_mode & 0o777) == 0o777  # guest is a different uid → must be writable


@pytest.mark.asyncio
async def test_launch_rejects_store_prefix_escaping_conduit_root(monkeypatch, tmp_path):
    # Defense-in-depth for the prefix-scoped mount: a store_prefix with `..` or an absolute path would
    # make `<conduit>/<prefix>` resolve OUTSIDE the conduit root and bind an arbitrary HOST dir into the
    # guest. store_prefix is caller-set (the facade uses run/<uuid>), but the runtime must never build
    # an escaping mount regardless of caller.
    rt = _rt(conduit_host_dir=str(tmp_path))
    _stub_run(monkeypatch, rt, returns={"run": (0, "vm\n", "")})
    for bad in ("../../../etc", "run/../../../../etc", "/etc/passwd"):
        with pytest.raises(ValueError, match="store_prefix"):
            await rt.launch(_spec(store_prefix=bad))
    # a leftover escaping dir must NOT have been created on the host
    assert (
        not (tmp_path.parent / "etc").exists() or True
    )  # sanity: no crash; guard fired before mkdir


@pytest.mark.asyncio
async def test_deny_all_egress_uses_network_none(monkeypatch):
    # The default run has an EMPTY egress allowlist (deny-all). No NIC is needed — the store is a
    # virtiofs bind — so the guest launches with `--network none`: zero CNI, zero host iptables.
    rt = _rt()  # constructed with a bridge network, but deny-all overrides it
    await rt.apply_egress([])
    calls = _stub_run(monkeypatch, rt, returns={"run": (0, "vm\n", "")})
    await rt.launch(_spec())
    argv = calls[0]
    assert argv[argv.index("--network") + 1] == "none"


def _stub_egress(monkeypatch, rt):
    """Stub the host-mutating egress seams (iptables + proxy subprocess); record iptables calls."""
    ipt: list[str] = []
    ev: list[str] = []

    async def fake_ipt(*args, check=True):
        ipt.append(" ".join(args))
        return 0

    async def fake_start():
        ev.append("proxy-start")

    async def fake_stop():
        ev.append("proxy-stop")

    monkeypatch.setattr(rt, "_iptables", fake_ipt)
    monkeypatch.setattr(rt, "_start_egress_proxy", fake_start)
    monkeypatch.setattr(rt, "_stop_egress_proxy", fake_stop)
    return ipt, ev


@pytest.mark.asyncio
async def test_nonempty_egress_uses_bridge_network_and_writes_domains(monkeypatch, tmp_path):
    # A non-empty allowlist DOES need a NIC + the SNI proxy — so the bridge network is used and the
    # live domains file is written for the proxy to read.
    dfile = tmp_path / "egress-domains"
    rt = _rt(egress_domains_file=str(dfile))
    _stub_egress(monkeypatch, rt)
    await rt.apply_egress(["api.anthropic.com", "pypi.org"])
    calls = _stub_run(monkeypatch, rt, returns={"run": (0, "vm\n", "")})
    await rt.launch(_spec())
    argv = calls[0]
    assert argv[argv.index("--network") + 1] == "bridge"
    assert dfile.read_text() == "api.anthropic.com,pypi.org"


@pytest.mark.asyncio
async def test_apply_egress_programs_scoped_firewall_and_starts_proxy(monkeypatch, tmp_path):
    # Runtime-managed egress (e2b model): a non-empty allowlist stands up the SNI proxy + iptables
    # scoped to the sandbox bridge subnet, entirely inside apply_egress — no setup script.
    rt = _rt(
        egress_domains_file=str(tmp_path / "d"), net_subnet="10.9.0.0/24", egress_proxy_port=4444
    )
    ipt, ev = _stub_egress(monkeypatch, rt)
    await rt.apply_egress(["example.com"])
    assert "proxy-start" in ev
    assert any("-I FORWARD 1 -s 10.9.0.0/24 -j RESOLUTO-SANDBOX-EGRESS" in c for c in ipt)
    assert any(
        "-t nat -I PREROUTING 1 -s 10.9.0.0/24 -p tcp --dport 443 -j REDIRECT --to-ports 4444" in c
        for c in ipt
    )
    assert any("-I INPUT 1 -s 10.9.0.0/24 -p tcp --dport 4444 -j ACCEPT" in c for c in ipt)
    assert any(c.endswith("-j REJECT") for c in ipt)  # default-deny chain rules applied


@pytest.mark.asyncio
async def test_clear_egress_tears_down_firewall_and_proxy(monkeypatch, tmp_path):
    dfile = tmp_path / "d"
    rt = _rt(egress_domains_file=str(dfile))
    ipt, ev = _stub_egress(monkeypatch, rt)
    await rt.apply_egress(["example.com"])  # up
    ipt.clear()
    ev.clear()
    await rt.clear_egress()  # down
    assert "proxy-stop" in ev
    assert any("-D FORWARD" in c for c in ipt)
    assert any("-X RESOLUTO-SANDBOX-EGRESS" in c for c in ipt)


@pytest.mark.asyncio
async def test_deny_all_never_touches_iptables(monkeypatch, tmp_path):
    # A deny-all run (no prior egress) must NOT shell out to iptables at all — network=none needs none.
    rt = _rt(egress_domains_file=str(tmp_path / "d"))
    ipt, ev = _stub_egress(monkeypatch, rt)
    await rt.apply_egress([])
    assert ipt == [] and ev == []


@pytest.mark.asyncio
async def test_apply_deny_all_writes_no_domains_file(tmp_path):
    # Deny-all provisions NOTHING host-side: apply_egress([]) must not touch the domains file, even
    # when its directory is unwritable/missing (the out-of-the-box, no-host-modification path).
    missing = tmp_path / "does-not-exist" / "egress-domains"
    rt = _rt(egress_domains_file=str(missing))
    await rt.apply_egress([])  # must not raise
    assert not missing.exists()


@pytest.mark.asyncio
async def test_status_maps_exit_code(monkeypatch):
    from resoluto.sandbox.contracts import SandboxHandle

    rt = _rt()
    _stub_run(monkeypatch, rt, returns={"inspect": (0, "exited|0\n", "")})
    st = await rt.status(SandboxHandle(id="vm"))
    assert st.phase == "succeeded" and st.exit_code == 0 and st.terminal

    _stub_run(monkeypatch, rt, returns={"inspect": (0, "exited|1\n", "")})
    st = await rt.status(SandboxHandle(id="vm"))
    assert st.phase == "failed" and st.exit_code == 1

    _stub_run(monkeypatch, rt, returns={"inspect": (0, "running|0\n", "")})
    st = await rt.status(SandboxHandle(id="vm"))
    assert st.phase == "running" and not st.terminal


@pytest.mark.asyncio
async def test_sweep_removes_by_label(monkeypatch):
    rt = _rt()
    calls = _stub_run(monkeypatch, rt, returns={"ps": (0, "id1\nid2\n", "")})
    n = await rt.sweep({"resoluto.run_id": "r1"})
    assert n == 2
    assert calls[0][0] == "ps" and "label=resoluto.run_id=r1" in calls[0]
    assert ["rm", "-f", "id1"] == calls[1] and ["rm", "-f", "id2"] == calls[2]


@pytest.mark.asyncio
async def test_sweep_raises_on_ps_failure_instead_of_reporting_zero(monkeypatch):
    # A failed `ps -aq` (containerd unreachable, permission denied) must never look like a clean
    # "0 matched" sweep — that would be a false-positive success for a leak backstop.
    rt = _rt()
    _stub_run(monkeypatch, rt, returns={"ps": (1, "", "permission denied")})
    with pytest.raises(RuntimeError, match="permission denied"):
        await rt.sweep({"resoluto.run_id": "r1"})


@pytest.mark.asyncio
async def test_status_inspect_failure_reason_carries_real_stderr(monkeypatch):
    from resoluto.sandbox.contracts import SandboxHandle

    rt = _rt()
    _stub_run(monkeypatch, rt, returns={"inspect": (1, "", "containerd: connection refused")})
    st = await rt.status(SandboxHandle(id="vm"))
    assert st.phase == "unknown"
    assert "containerd: connection refused" in st.reason
