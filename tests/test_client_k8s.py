"""Integration test: Sandbox(backend='k8s').run() round-trips through a real Kata pod."""
import os

import pytest

from resoluto_sandbox import Sandbox


@pytest.mark.integration
def test_k8s_run_roundtrips(tmp_path):
    image = os.environ["RESOLUTO_LANE_IMAGE"]
    sb = Sandbox(backend="k8s", image=image)
    out = sb.run(["bash", "-lc", "echo hi > out.txt && echo done"],
                 workspace=str(tmp_path), output_paths=["out.txt"])
    assert out.exit_code == 0, out
    assert any(p.endswith("out.txt") for p in out.artifacts)
    assert (tmp_path / "out.txt").read_text().strip() == "hi"
