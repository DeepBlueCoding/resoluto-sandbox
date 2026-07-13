"""A SandboxRuntime that launches each sandbox as a Kata microVM via nerdctl against a dedicated containerd."""

from __future__ import annotations

import asyncio
import contextlib
import os
import sys
import tempfile

from resoluto.sandbox.contracts import (
    SandboxHandle,
    SandboxLaunchSpec,
    SandboxRuntime,
    SandboxStatus,
    check_runtime_class_guard,
)

_PHASE_MAP = {
    "created": "pending",
    "running": "running",
    "paused": "running",
    "restarting": "running",
    "removing": "running",
    "exited": "exited",
    "dead": "failed",
}


def _resolve_sudo() -> bool:
    """Returns whether nerdctl must run via sudo -n."""
    v = os.environ.get("RESOLUTO_LOCAL_NERDCTL_SUDO")
    if v is not None:
        return v.strip().lower() not in ("", "0", "false", "no")
    return os.geteuid() != 0


class KataNerdctlSandboxRuntime(SandboxRuntime):
    """Launches each sandbox as a Kata microVM via nerdctl against a dedicated containerd."""

    def __init__(
        self,
        *,
        address: str,
        namespace: str,
        conduit_host_dir: str,
        conduit_mount: str = "/conduit",
        runtime: str = "io.containerd.kata.v2",
        cni_path: str | None = None,
        cni_netconfpath: str | None = None,
        network: str = "bridge",
        nerdctl: str = "nerdctl",
        sudo: bool = False,
        egress_domains_file: str | None = None,
        net_subnet: str = "10.222.0.0/24",
        egress_proxy_port: int = 3129,
        dind_graph_dir: str = "/var/lib/resoluto-local/dind-graph",
    ) -> None:
        check_runtime_class_guard(runtime)
        self._address = address
        self._namespace = namespace
        self._conduit_host_dir = conduit_host_dir
        self._conduit_mount = conduit_mount
        self._runtime = runtime
        self._cni_path = cni_path
        self._cni_netconfpath = cni_netconfpath
        self._network = network
        self._nerdctl = nerdctl
        self._sudo = sudo
        # Runtime-managed per-run egress (e2b model): a non-empty allowlist starts a SNI proxy + scoped
        # iptables HERE, torn down when the run ends — no setup script, nothing persistent. The SNI
        # allowlist file the proxy reads live; defaults to a per-process temp path we can write nonroot.
        self._egress_domains_file = egress_domains_file or os.path.join(
            tempfile.gettempdir(), f"resoluto-egress-{os.getpid()}.domains"
        )
        self._net_subnet = net_subnet
        self._egress_proxy_port = egress_proxy_port
        self._egress_chain = "RESOLUTO-SANDBOX-EGRESS"
        self._egress_proxy_proc: "asyncio.subprocess.Process | None" = None
        self._egress_active = False
        # The active egress allowlist for THIS run, set by apply_egress. None = never applied (use the
        # configured network); [] = deny-all (launch with --network none — no NIC, no host firewall);
        # non-empty = allowlist (needs the bridge + SNI proxy).
        self._active_egress: list[str] | None = None
        # Base dir (on real DISK, never /run tmpfs) for a block-backed dind graph: each dind step
        # binds its own subdir at /var/lib/docker so image layers live on disk, keeping RAM free.
        self._dind_graph_dir = dind_graph_dir
        self._graph_dirs: dict[str, str] = {}  # container id → its host graph dir (for cleanup)

    async def apply_egress(self, domains: "list[str] | None") -> None:
        """Set THIS run's egress. Deny-all (empty/None) provisions NOTHING host-side — the guest
        launches `--network none` (no NIC, no firewall). A non-empty allowlist stands up the egress
        enforcement HERE, per run: a SNI proxy + iptables scoped to the sandbox bridge subnet. No setup
        script, nothing persistent — `clear_egress`/`aclose` tears it all down."""
        self._active_egress = [d.strip() for d in (domains or []) if d.strip()]
        if self._active_egress:
            await self._egress_apply(self._active_egress)
        else:
            await self._egress_teardown()

    async def clear_egress(self) -> None:
        """Tear down this run's egress enforcement (proxy + iptables + domains file). Deny-all."""
        self._active_egress = []
        await self._egress_teardown()

    async def aclose(self) -> None:
        """Best-effort teardown of any lingering egress state when the runtime is closed."""
        await self._egress_teardown()

    async def _iptables(self, *args: str, check: bool = True) -> int:
        """Run `sudo iptables <args>`; return rc. check=False swallows a nonzero rc (for idempotent
        `-D`/`-X` teardown that may hit an already-absent rule/chain)."""
        argv = (["sudo", "-n"] if self._sudo else []) + ["iptables", *args]
        proc = await asyncio.create_subprocess_exec(
            *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        _, err = await proc.communicate()
        rc = proc.returncode if proc.returncode is not None else -1
        if check and rc != 0:
            raise RuntimeError(
                f"iptables {' '.join(args)} failed (rc={rc}): {err.decode().strip()}"
            )
        return rc

    async def _start_egress_proxy(self) -> None:
        """Launch the SNI proxy (nonroot; port > 1024) reading this run's live domains file."""
        self._egress_proxy_proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "resoluto.sandbox.egress_proxy",
            "--host",
            "0.0.0.0",
            "--port",
            str(self._egress_proxy_port),
            "--domains-file",
            self._egress_domains_file,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )

    async def _stop_egress_proxy(self) -> None:
        proc, self._egress_proxy_proc = self._egress_proxy_proc, None
        if proc is None or proc.returncode is not None:
            return
        with contextlib.suppress(ProcessLookupError):
            proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=5)
        except (asyncio.TimeoutError, Exception):  # noqa: BLE001
            with contextlib.suppress(Exception):
                proc.kill()

    async def _egress_apply(self, domains: list[str]) -> None:
        """Stand up per-run egress: SNI proxy + iptables (chain + FORWARD jump + NAT :443 REDIRECT +
        INPUT accept), scoped to the sandbox bridge subnet. Sweeps any stale state first (crash-safe)."""
        from resoluto.sandbox.egress import EgressConfig, local_egress_iptables

        await self._egress_teardown(force=True)  # clean slate / stale sweep
        with open(self._egress_domains_file, "w", encoding="utf-8") as f:
            f.write(",".join(domains))
        await self._start_egress_proxy()
        self._egress_active = True

        chain, sub, port = self._egress_chain, self._net_subnet, str(self._egress_proxy_port)
        if await self._iptables("-N", chain, check=False) != 0:
            await self._iptables("-F", chain)  # chain existed → flush to a known state
        # The chain is DNS + denies + default-REJECT; :443 is diverted to the SNI proxy (which matches
        # by domain) via the NAT REDIRECT below, so no per-domain allow rules live in the chain.
        for rule in local_egress_iptables(EgressConfig(allow=()), chain=chain):
            await self._iptables(*rule)
        await self._iptables("-I", "FORWARD", "1", "-s", sub, "-j", chain)
        await self._iptables(
            "-t", "nat", "-I", "PREROUTING", "1", "-s", sub,
            "-p", "tcp", "--dport", "443", "-j", "REDIRECT", "--to-ports", port,
        )  # fmt: skip
        await self._iptables(
            "-I", "INPUT", "1", "-s", sub, "-p", "tcp", "--dport", port, "-j", "ACCEPT"
        )

    async def _egress_teardown(self, *, force: bool = False) -> None:
        """Remove all per-run egress state. No-op unless egress was applied (or force=True for the
        pre-apply stale sweep) — so a deny-all run never shells out to iptables."""
        if not (self._egress_active or force):
            return
        self._egress_active = False
        await self._stop_egress_proxy()
        chain, sub, port = self._egress_chain, self._net_subnet, str(self._egress_proxy_port)
        await self._iptables(
            "-t", "nat", "-D", "PREROUTING", "-s", sub,
            "-p", "tcp", "--dport", "443", "-j", "REDIRECT", "--to-ports", port, check=False,
        )  # fmt: skip
        await self._iptables(
            "-D", "INPUT", "-s", sub, "-p", "tcp", "--dport", port, "-j", "ACCEPT", check=False
        )
        await self._iptables("-D", "FORWARD", "-s", sub, "-j", chain, check=False)
        await self._iptables("-F", chain, check=False)
        await self._iptables("-X", chain, check=False)
        with contextlib.suppress(OSError):
            os.remove(self._egress_domains_file)

    @classmethod
    def from_env(
        cls, *, conduit_host_dir: str, conduit_mount: str = "/conduit"
    ) -> "KataNerdctlSandboxRuntime":
        """Builds an instance from the RESOLUTO_LOCAL_* environment knobs."""
        return cls(
            address=os.environ.get(
                "RESOLUTO_LOCAL_CONTAINERD_ADDRESS",
                "/run/resoluto-local/containerd/containerd.sock",
            ),
            namespace=os.environ.get("RESOLUTO_LOCAL_CONTAINERD_NAMESPACE", "resoluto-local"),
            conduit_host_dir=conduit_host_dir,
            conduit_mount=conduit_mount,
            runtime=os.environ.get("RESOLUTO_LOCAL_KATA_RUNTIME", "io.containerd.kata.v2"),
            cni_path=os.environ.get("RESOLUTO_LOCAL_CNI_PATH", "/opt/resoluto-local/libexec/cni"),
            cni_netconfpath=os.environ.get(
                "RESOLUTO_LOCAL_CNI_NETCONFPATH", "/etc/resoluto-local/cni/net.d"
            ),
            network=os.environ.get("RESOLUTO_LOCAL_NETWORK", "resoluto-local"),
            nerdctl=os.environ.get("RESOLUTO_LOCAL_NERDCTL", "/opt/resoluto-local/bin/nerdctl"),
            sudo=_resolve_sudo(),
            egress_domains_file=os.environ.get("RESOLUTO_LOCAL_EGRESS_DOMAINS_FILE"),
            net_subnet=os.environ.get("RESOLUTO_LOCAL_NET_SUBNET", "10.222.0.0/24"),
            egress_proxy_port=int(os.environ.get("RESOLUTO_EGRESS_PROXY_PORT", "3129")),
            dind_graph_dir=os.environ.get(
                "RESOLUTO_LOCAL_DIND_GRAPH_DIR", "/var/lib/resoluto-local/dind-graph"
            ),
        )

    def _base(self) -> list[str]:
        argv = ["sudo", "-n"] if self._sudo else []
        argv += [self._nerdctl, "--address", self._address, "--namespace", self._namespace]
        if self._cni_path:
            argv += ["--cni-path", self._cni_path]
        if self._cni_netconfpath:
            argv += ["--cni-netconfpath", self._cni_netconfpath]
        return argv

    async def _run(self, *args: str) -> tuple[int, str, str]:
        """Runs `nerdctl <args>` and returns (rc, stdout, stderr)."""
        proc = await asyncio.create_subprocess_exec(
            *self._base(),
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await proc.communicate()
        rc = proc.returncode if proc.returncode is not None else -1
        return rc, out.decode(), err.decode()

    async def launch(self, spec: SandboxLaunchSpec) -> SandboxHandle:
        # Deny-all (an applied, empty allowlist) needs no NIC: --network none means zero CNI and zero
        # host firewall. Only a non-empty allowlist (or a never-applied runtime) uses the bridge.
        network = "none" if self._active_egress == [] else self._network
        argv: list[str] = ["run", "-d", "--runtime", self._runtime, "--network", network]
        for k, v in spec.labels.items():
            argv += ["--label", f"{k}={v}"]
        for k, v in spec.env.items():
            argv += ["-e", f"{k}={v}"]
        # Scope the conduit mount to THIS run's prefix: the guest sees only `<mount>/<prefix>`, never
        # sibling runs/lanes that share the same conduit root (the store is the guest→host seam, so an
        # over-broad mount = cross-run read + write). The host still keys its own reads on full prefixes
        # against conduit_host_dir, so resume/tailing/fetch are unaffected. The substrate pre-creates
        # the (world-writable) prefix dir before launch; absent a prefix, mount the whole conduit.
        if spec.store_prefix:
            # Defense-in-depth: never build an escaping mount. A store_prefix with `..` or an absolute
            # component would make `<conduit>/<prefix>` resolve OUTSIDE the conduit root and bind an
            # arbitrary HOST directory into the guest. Reject before any path/makedirs.
            from pathlib import PurePosixPath

            _pp = PurePosixPath(spec.store_prefix)
            if _pp.is_absolute() or ".." in _pp.parts:
                raise ValueError(
                    f"unsafe store_prefix escapes the conduit root: {spec.store_prefix!r}"
                )
            src = f"{self._conduit_host_dir}/{spec.store_prefix}"
            dst = f"{self._conduit_mount}/{spec.store_prefix}"
            # Self-contained: create the (world-writable) mount source HERE, not in a caller, so the
            # contract travels with the runtime. Both callers (the SubstrateBackend facade AND the
            # engine's lane substrate) inject this same runtime; without an existing source, nerdctl
            # auto-creates it root-owned and the guest (a different uid) can't write its own telemetry.
            # Guarded on the conduit root existing so unit tests with a synthetic root don't touch disk.
            if os.path.isdir(self._conduit_host_dir):
                os.makedirs(src, exist_ok=True)
                os.chmod(src, 0o777)
        else:
            src, dst = self._conduit_host_dir, self._conduit_mount
        argv += ["-v", f"{src}:{dst}"]
        res = spec.resources
        argv += ["--memory", str(res.memory_bytes), "--memory-swap", str(res.memory_bytes)]
        argv += ["--cpus", str(res.cpu_cores)]
        if spec.privileged:
            # Guest-scoped privilege under Kata: grant extended privileges for docker-in-docker but
            # do NOT bind host devices. The Kata guest already owns the default device nodes
            # (/dev/full etc.); nerdctl's plain --privileged re-creates them in the guest and the
            # shim fails with `Creating container device /dev/full — EEXIST`. This is the nerdctl
            # equivalent of the k8s runtime's privileged_without_host_devices.
            argv += [
                "--privileged",
                "--security-opt",
                "privileged-without-host-devices=true",
                "--user",
                "0",
            ]
            graph_dir = self._graph_dir_for(spec) if res.graph_backend == "block" else None
            if graph_dir is not None:
                # Disk-backed graph: bind a per-step host dir (real disk) at /var/lib/docker so
                # dockerd's image layers stay OFF RAM. nerdctl creates the bind source as root.
                argv += ["-v", f"{graph_dir}:/var/lib/docker"]
            elif res.dind_graph_bytes is not None:
                argv += ["--tmpfs", f"/var/lib/docker:size={res.dind_graph_bytes}"]
        argv += [spec.image]
        argv += list(spec.args or spec.command or [])

        rc, out, err = await self._run(*argv)
        if rc != 0:
            raise RuntimeError(f"nerdctl run failed (rc={rc}): {err.strip() or out.strip()}")
        cid = out.strip().splitlines()[-1]
        if spec.privileged and spec.resources.graph_backend == "block":
            self._graph_dirs[cid] = self._graph_dir_for(spec)
        return SandboxHandle(id=cid, labels=spec.labels)

    def _graph_dir_for(self, spec: SandboxLaunchSpec) -> str:
        """The per-step host graph dir for a block-backed dind step — deterministic from the
        step's store prefix (unique per step) so it never collides with another live step."""
        leaf = (spec.store_prefix or "step").replace("/", "_").replace(":", "_")
        return os.path.join(self._dind_graph_dir, leaf)

    async def status(self, handle: SandboxHandle) -> SandboxStatus:
        rc, out, err = await self._run(
            "inspect", "--format", "{{.State.Status}}|{{.State.ExitCode}}", handle.id
        )
        if rc != 0:
            # The real stderr distinguishes "container genuinely gone" from an infra failure
            # (containerd unreachable, permission denied) — don't collapse both into one fixed string.
            return SandboxStatus(
                phase="unknown", reason=f"inspect failed (rc={rc}): {err.strip() or out.strip()}"
            )
        raw_status, _, raw_code = out.strip().partition("|")
        mapped = _PHASE_MAP.get(raw_status, "unknown")
        if mapped == "exited":
            code = int(raw_code) if raw_code.strip().lstrip("-").isdigit() else None
            phase = "succeeded" if code == 0 else "failed"
            return SandboxStatus(phase=phase, exit_code=code, reason=raw_status)
        return SandboxStatus(phase=mapped, reason=raw_status)

    async def destroy(self, handle: SandboxHandle) -> None:
        await self._run("rm", "-f", handle.id)
        await self._rm_graph_dir(handle.id)

    async def _rm_graph_dir(self, cid: str) -> None:
        """Remove a block dind step's disk graph dir after the container is gone (root-owned via
        the guest, so remove with the same sudo escalation nerdctl uses). No-op for tmpfs steps."""
        graph_dir = self._graph_dirs.pop(cid, None)
        if not graph_dir:
            return
        cmd = (["sudo", "-n"] if self._sudo else []) + ["rm", "-rf", graph_dir]
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        await proc.communicate()

    async def sweep(self, labels: dict[str, str]) -> int:
        argv = ["ps", "-aq"]
        for k, v in labels.items():
            argv += ["--filter", f"label={k}={v}"]
        rc, out, err = await self._run(*argv)
        if rc != 0:
            # A failure to even list containers (containerd unreachable, permission denied) must
            # never look like "0 matched" — that's a false-positive clean sweep for a leak backstop.
            raise RuntimeError(f"nerdctl ps failed (rc={rc}): {err.strip() or out.strip()}")
        ids = [line for line in out.split() if line]
        for cid in ids:
            await self._run("rm", "-f", cid)
        return len(ids)

    async def logs(self, handle: SandboxHandle, *, tail: int = 200) -> str:
        rc, out, err = await self._run("logs", "--tail", str(tail), handle.id)
        if rc != 0:
            return f"(logs unavailable: {err.strip()})"
        return out + err
