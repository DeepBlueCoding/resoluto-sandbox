"""Verify a plain program runs unchanged via Sandbox — now over the Docker local
backend (staged workspace, container python). The program never imports
resoluto_sandbox; its stdout is reconstructed from the store-mediated telemetry."""
import subprocess

import pytest

from resoluto_sandbox import Sandbox

_IMAGE = "resoluto-sandbox-base:0.1.0"


def _image_present(ref: str) -> bool:
    return subprocess.run(["docker", "image", "inspect", ref],
                          capture_output=True).returncode == 0


@pytest.mark.integration
def test_same_program_runs_unchanged_via_sandbox(tmp_path):
    if not _image_present(_IMAGE):
        pytest.skip(f"local image {_IMAGE} not present")
    script = tmp_path / "echo_prompt.py"
    script.write_text("import sys; print('OUT:' + (sys.argv[1] if len(sys.argv) > 1 else ''))")
    via = Sandbox(backend="local", image=_IMAGE).run(
        ["python", "echo_prompt.py", "hi"],
        workspace=str(tmp_path),
    ).output
    assert "OUT:hi" in via
