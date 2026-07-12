"""A SandboxRuntime that launches each sandbox as a Kata microVM via nerdctl against a dedicated containerd."""

from __future__ import annotations

import asyncio
import os

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
        # the live SNI allowlist file the persistent egress proxy reads (set per-run, see apply_egress)
        self._egress_domains_file = egress_domains_file
        # The active egress allowlist for THIS run, set by apply_egress. None = never applied (use the
        # configured network); [] = deny-all (launch with --network none — no NIC, no host firewall);
        # non-empty = allowlist (needs the bridge + SNI proxy).
        self._active_egress: list[str] | None = None
        # Base dir (on real DISK, never /run tmpfs) for a block-backed dind graph: each dind step
        # binds its own subdir at /var/lib/docker so image layers live on disk, keeping RAM free.
        self._dind_graph_dir = dind_graph_dir
        self._graph_dirs: dict[str, str] = {}  # container id → its host graph dir (for cleanup)

    async def apply_egress(self, domains: "list[str] | None") -> None:
        """Set THIS run's SNI egress allowlist. Deny-all (empty/None) provisions NOTHING host-side —
        the guest launches with `--network none` (see `launch`), so there is no proxy to feed and no
        domains file to write. A non-empty allowlist writes the proxy's live domains file (per-run,
        no re-provision). Idempotent."""
        self._active_egress = [d.strip() for d in (domains or []) if d.strip()]
        if not self._egress_domains_file:
            return
        if self._active_egress:
            with open(self._egress_domains_file, "w", encoding="utf-8") as f:
                f.write(",".join(self._active_egress))
        elif os.path.exists(self._egress_domains_file):
            # Reset a stale allowlist back to deny; never CREATE the file for deny-all (no NIC needs it).
            with open(self._egress_domains_file, "w", encoding="utf-8") as f:
                f.write("")

    async def clear_egress(self) -> None:
        """Reset this run's egress allowlist to deny-all (write the domains file empty)."""
        await self.apply_egress([])

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
            egress_domains_file=os.environ.get(
                "RESOLUTO_LOCAL_EGRESS_DOMAINS_FILE", "/run/resoluto-local/egress-domains"
            ),
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
            src = f"{self._conduit_host_dir}/{spec.store_prefix}"
            dst = f"{self._conduit_mount}/{spec.store_prefix}"
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
