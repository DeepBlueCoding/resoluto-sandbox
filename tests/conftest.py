"""Integration-test fixtures — make the pod tests self-contained.

The Kata pod tests run with `imagePullPolicy=Never` against LOCAL-only images
(there's no registry in the spike env). k3s evicts registry-less imported images
between runs, which previously left pods stuck `Pending` with `ErrImageNeverPull`.
These fixtures guarantee the image is present in k3s containerd right before each
test that needs it — so the suite owns its precondition instead of relying on a
manual `ctr import` that may have been GC'd. The runner image is also (re)built
from the current source, so the e2e always exercises HEAD, never a stale image.
"""
from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

DIND_DOCKER = "docker:27-dind"
DIND_CTR = "docker.io/library/docker:27-dind"
RUNNER_DOCKER = "resoluto-sandbox-runner:dev"
RUNNER_CTR = "docker.io/library/resoluto-sandbox-runner:dev"


def _run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def _docker_has(ref: str) -> bool:
    return _run(["docker", "image", "inspect", ref]).returncode == 0


def _containerd_has(ctr_ref: str) -> bool:
    return ctr_ref in _run(["sudo", "k3s", "ctr", "images", "ls", "-q"]).stdout


def _import_into_k3s(docker_ref: str) -> None:
    """docker → k3s containerd. Fail loud — a missing image is a real precondition
    failure, not something to silently skip."""
    fd, path = tempfile.mkstemp(suffix=".tar")
    os.close(fd)
    try:
        save = _run(["docker", "save", docker_ref, "-o", path])
        assert save.returncode == 0, f"docker save {docker_ref} failed: {save.stderr}"
        imp = _run(["sudo", "k3s", "ctr", "images", "import", path])
        assert imp.returncode == 0, f"k3s ctr import {docker_ref} failed: {imp.stderr}"
    finally:
        os.unlink(path)


def _ensure_in_k3s(docker_ref: str, ctr_ref: str) -> str:
    if not _containerd_has(ctr_ref):
        _import_into_k3s(docker_ref)
        assert _containerd_has(ctr_ref), f"{ctr_ref} still absent after import"
    return ctr_ref


@pytest.fixture(scope="session")
def _runner_built() -> str:
    """Build the runner image from the CURRENT source once per session."""
    build = _run(
        ["docker", "build", "-f", "Dockerfile.runner", "-t", RUNNER_DOCKER, "."],
        cwd=str(REPO_ROOT),
    )
    assert build.returncode == 0, f"runner image build failed:\n{build.stderr[-2000:]}"
    return RUNNER_DOCKER


@pytest.fixture
def runner_image(_runner_built) -> str:
    """Ensure the freshly-built runner image is in k3s containerd at test time."""
    return _ensure_in_k3s(RUNNER_DOCKER, RUNNER_CTR)


@pytest.fixture
def dind_image() -> str:
    """Ensure docker:27-dind is pulled and present in k3s containerd at test time."""
    if not _docker_has(DIND_DOCKER):
        assert _run(["docker", "pull", DIND_DOCKER]).returncode == 0, "docker pull docker:27-dind failed"
    return _ensure_in_k3s(DIND_DOCKER, DIND_CTR)
