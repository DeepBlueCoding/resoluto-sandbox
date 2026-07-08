"""Integration test: Sandbox(backend='local') round-trips through a REAL Kata microVM
(nerdctl + dedicated standalone containerd) sharing a LocalConduit bind mount (no cluster, no S3)."""
import os

import pytest

from resoluto.sandbox import Sandbox

_IMAGE = "localhost:5000/resoluto-lane:0.1.0"
_CONTAINERD_SOCK = "/run/resoluto-local/containerd/containerd.sock"


def _local_stack_ready() -> bool:
    return os.path.exists(_CONTAINERD_SOCK) and os.path.exists("/opt/resoluto-local/bin/nerdctl")


@pytest.mark.integration
def test_local_kata_roundtrips(tmp_path):
    if not _local_stack_ready():
        pytest.skip("local Kata backend not provisioned (run scripts/local-backend-up.sh)")
    sb = Sandbox(backend="local", image=_IMAGE)
    out = sb.run(
        ["bash", "-lc", "echo hi > out.txt && echo done"],
        workspace=str(tmp_path),
        output_paths=["out.txt"],
    )
    assert out.exit_code == 0, out
    assert "done" in out.output, out
    assert any(p.endswith("out.txt") for p in out.artifacts), out
    assert (tmp_path / "out.txt").read_text().strip() == "hi"
