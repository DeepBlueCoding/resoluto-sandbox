"""A SandboxRuntime that launches each sandbox as a Kata microVM via nerdctl against a dedicated containerd."""
from __future__ import annotations

import asyncio

import os

from resoluto_sandbox.contracts import (
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

    async def apply_egress(self, domains: "list[str] | None") -> None:
        """Set THIS run's SNI egress allowlist by writing the proxy's live domains file — per-step
        networking with no re-provision. Empty/None = deny all (secure). Idempotent; fail-fast if the
        proxy file dir is missing (run scripts/local-backend-up.sh once to set it up)."""
        if not self._egress_domains_file:
            return
        text = ",".join(d.strip() for d in (domains or []) if d.strip())
        with open(self._egress_domains_file, "w", encoding="utf-8") as f:
            f.write(text)

    async def clear_egress(self) -> None:
        """Reset this run's egress allowlist to deny-all (write the domains file empty)."""
        await self.apply_egress([])

    @classmethod
    def from_env(cls, *, conduit_host_dir: str, conduit_mount: str = "/conduit") -> "KataNerdctlSandboxRuntime":
        """Builds an instance from the RESOLUTO_LOCAL_* environment knobs."""
        return cls(
            address=os.environ.get("RESOLUTO_LOCAL_CONTAINERD_ADDRESS",
                                   "/run/resoluto-local/containerd/containerd.sock"),
            namespace=os.environ.get("RESOLUTO_LOCAL_CONTAINERD_NAMESPACE", "resoluto-local"),
            conduit_host_dir=conduit_host_dir, conduit_mount=conduit_mount,
            runtime=os.environ.get("RESOLUTO_LOCAL_KATA_RUNTIME", "io.containerd.kata.v2"),
            cni_path=os.environ.get("RESOLUTO_LOCAL_CNI_PATH", "/opt/resoluto-local/libexec/cni"),
            cni_netconfpath=os.environ.get("RESOLUTO_LOCAL_CNI_NETCONFPATH", "/etc/resoluto-local/cni/net.d"),
            network=os.environ.get("RESOLUTO_LOCAL_NETWORK", "resoluto-local"),
            nerdctl=os.environ.get("RESOLUTO_LOCAL_NERDCTL", "/opt/resoluto-local/bin/nerdctl"),
            sudo=_resolve_sudo(),
            egress_domains_file=os.environ.get("RESOLUTO_LOCAL_EGRESS_DOMAINS_FILE",
                                               "/run/resoluto-local/egress-domains"),
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
            *self._base(), *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await proc.communicate()
        rc = proc.returncode if proc.returncode is not None else -1
        return rc, out.decode(), err.decode()

    async def launch(self, spec: SandboxLaunchSpec) -> SandboxHandle:
        argv: list[str] = ["run", "-d", "--runtime", self._runtime, "--network", self._network]
        for k, v in spec.labels.items():
            argv += ["--label", f"{k}={v}"]
        for k, v in spec.env.items():
            argv += ["-e", f"{k}={v}"]
        argv += ["-v", f"{self._conduit_host_dir}:{self._conduit_mount}"]
        res = spec.resources
        argv += ["--memory", str(res.memory_bytes), "--memory-swap", str(res.memory_bytes)]
        argv += ["--cpus", str(res.cpu_cores)]
        if spec.privileged:
            # Guest-scoped privilege under Kata: grant extended privileges for docker-in-docker but
            # do NOT bind host devices. The Kata guest already owns the default device nodes
            # (/dev/full etc.); nerdctl's plain --privileged re-creates them in the guest and the
            # shim fails with `Creating container device /dev/full — EEXIST`. This is the nerdctl
            # equivalent of the k8s runtime's privileged_without_host_devices.
            argv += ["--privileged", "--security-opt", "privileged-without-host-devices=true", "--user", "0"]
            if res.dind_graph_bytes is not None:
                argv += ["--tmpfs", f"/var/lib/docker:size={res.dind_graph_bytes}"]
        argv += [spec.image]
        argv += list(spec.args or spec.command or [])

        rc, out, err = await self._run(*argv)
        if rc != 0:
            raise RuntimeError(f"nerdctl run failed (rc={rc}): {err.strip() or out.strip()}")
        return SandboxHandle(id=out.strip().splitlines()[-1], labels=spec.labels)

    async def status(self, handle: SandboxHandle) -> SandboxStatus:
        rc, out, err = await self._run(
            "inspect", "--format", "{{.State.Status}}|{{.State.ExitCode}}", handle.id
        )
        if rc != 0:
            return SandboxStatus(phase="unknown", reason="container not found")
        raw_status, _, raw_code = out.strip().partition("|")
        mapped = _PHASE_MAP.get(raw_status, "unknown")
        if mapped == "exited":
            code = int(raw_code) if raw_code.strip().lstrip("-").isdigit() else None
            phase = "succeeded" if code == 0 else "failed"
            return SandboxStatus(phase=phase, exit_code=code, reason=raw_status)
        return SandboxStatus(phase=mapped, reason=raw_status)

    async def destroy(self, handle: SandboxHandle) -> None:
        await self._run("rm", "-f", handle.id)

    async def sweep(self, labels: dict[str, str]) -> int:
        argv = ["ps", "-aq"]
        for k, v in labels.items():
            argv += ["--filter", f"label={k}={v}"]
        rc, out, _ = await self._run(*argv)
        if rc != 0:
            return 0
        ids = [line for line in out.split() if line]
        for cid in ids:
            await self._run("rm", "-f", cid)
        return len(ids)

    async def logs(self, handle: SandboxHandle, *, tail: int = 200) -> str:
        rc, out, err = await self._run("logs", "--tail", str(tail), handle.id)
        if rc != 0:
            return f"(logs unavailable: {err.strip()})"
        return out + err
