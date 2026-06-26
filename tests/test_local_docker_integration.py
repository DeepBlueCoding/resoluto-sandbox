"""Integration test: Sandbox(backend='docker') round-trips through a REAL Docker
container sharing a LocalConduit bind mount (no cluster, no S3)."""
import subprocess

import pytest

from resoluto_sandbox import Sandbox

_IMAGE = "resoluto-sandbox-base:0.1.0"


def _image_present(ref: str) -> bool:
    return subprocess.run(["docker", "image", "inspect", ref],
                          capture_output=True).returncode == 0


@pytest.mark.integration
def test_local_docker_roundtrips(tmp_path):
    if not _image_present(_IMAGE):
        pytest.skip(f"local image {_IMAGE} not present (docker images | grep resoluto-sandbox)")
    sb = Sandbox(backend="docker", image=_IMAGE)
    out = sb.run(
        ["bash", "-lc", "echo hi > out.txt && echo done"],
        workspace=str(tmp_path),
        output_paths=["out.txt"],
    )
    assert out.exit_code == 0, out
    assert "done" in out.output, out
    assert any(p.endswith("out.txt") for p in out.artifacts), out
    assert (tmp_path / "out.txt").read_text().strip() == "hi"
