"""DockerSandboxRuntime — a `SandboxRuntime` over the local `docker` CLI.

The same launch/status/destroy/sweep/logs surface the k8s runtime implements, but
backed by `docker run`/`docker inspect`/`docker rm` invoked through
`asyncio.create_subprocess_exec` (NO python docker SDK — keep the core dependency-
light; docker is a host system dep). The container runs the SAME `runner_main` a
Kata pod runs; comms is store-mediated over a bind mount: the host's LocalConduit
dir (`conduit_host_dir`) is mounted into the container at `conduit_mount`, so the
in-container LocalConduit (pointed there by RESOLUTO_STORE_*) shares the physical
store with the host — no S3, no cluster.
"""
from __future__ import annotations

import asyncio

from resoluto_sandbox.contracts import (
    SandboxHandle,
    SandboxLaunchSpec,
    SandboxRuntime,
    SandboxStatus,
)

_PHASE_MAP = {
    "created": "pending",
    "running": "running",
    "paused": "running",
    "restarting": "running",
    "removing": "running",
    "exited": "exited",  # resolved to succeeded/failed by exit code
    "dead": "failed",
}


async def _docker(*args: str) -> tuple[int, str, str]:
    """Run `docker <args>` and return (returncode, stdout, stderr). No wall-clock timeout."""
    proc = await asyncio.create_subprocess_exec(
        "docker", *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate()
    return proc.returncode if proc.returncode is not None else -1, out.decode(), err.decode()


class DockerSandboxRuntime(SandboxRuntime):
    """Launch each sandbox as an isolated Docker container sharing a conduit bind mount.

    conduit_host_dir: host path of the LocalConduit root (mounted into the container).
    conduit_mount:    in-container mount path the container's LocalConduit points at.
    network:          optional docker network name (None = default bridge).
    """

    def __init__(
        self,
        *,
        conduit_host_dir: str,
        conduit_mount: str = "/conduit",
        network: str | None = None,
    ) -> None:
        self._conduit_host_dir = conduit_host_dir
        self._conduit_mount = conduit_mount
        self._network = network

    async def launch(self, spec: SandboxLaunchSpec) -> SandboxHandle:
        argv: list[str] = ["run", "-d"]
        for k, v in spec.labels.items():
            argv += ["--label", f"{k}={v}"]
        for k, v in spec.env.items():
            argv += ["-e", f"{k}={v}"]
        argv += ["-v", f"{self._conduit_host_dir}:{self._conduit_mount}"]
        if self._network:
            argv += ["--network", self._network]
        if spec.privileged:
            argv += ["--privileged"]
        argv += [spec.image]
        argv += list(spec.args or spec.command or [])

        rc, out, err = await _docker(*argv)
        if rc != 0:
            raise RuntimeError(f"docker run failed (rc={rc}): {err.strip() or out.strip()}")
        return SandboxHandle(id=out.strip(), labels=spec.labels)

    async def status(self, handle: SandboxHandle) -> SandboxStatus:
        rc, out, err = await _docker(
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
        await _docker("rm", "-f", handle.id)

    async def sweep(self, labels: dict[str, str]) -> int:
        argv = ["ps", "-aq"]
        for k, v in labels.items():
            argv += ["--filter", f"label={k}={v}"]
        rc, out, _ = await _docker(*argv)
        if rc != 0:
            return 0
        ids = [line for line in out.split() if line]
        for cid in ids:
            await _docker("rm", "-f", cid)
        return len(ids)

    async def logs(self, handle: SandboxHandle, *, tail: int = 200) -> str:
        rc, out, err = await _docker("logs", "--tail", str(tail), handle.id)
        if rc != 0:
            return f"(logs unavailable: {err.strip()})"
        return out + err
