"""Verify a plain program runs unchanged via Sandbox — now over the local Kata backend
(staged workspace, microVM python). The program never imports resoluto_sandbox; its stdout
is reconstructed from the store-mediated telemetry."""
import os

import pytest

from resoluto_sandbox import Sandbox

_IMAGE = "localhost:5000/resoluto-lane:dev"
_CONTAINERD_SOCK = "/run/resoluto-local/containerd/containerd.sock"


def _local_stack_ready() -> bool:
    """The dedicated local-backend containerd (set up by scripts/local-backend-up.sh) must be up."""
    return os.path.exists(_CONTAINERD_SOCK) and os.path.exists("/opt/resoluto-local/bin/nerdctl")


@pytest.mark.integration
def test_same_program_runs_unchanged_via_sandbox(tmp_path):
    if not _local_stack_ready():
        pytest.skip("local Kata backend not provisioned (run scripts/local-backend-up.sh)")
    script = tmp_path / "echo_prompt.py"
    script.write_text("import sys; print('OUT:' + (sys.argv[1] if len(sys.argv) > 1 else ''))")
    via = Sandbox(backend="local", image=_IMAGE).run(
        ["python", "echo_prompt.py", "hi"],
        workspace=str(tmp_path),
    ).output
    assert "OUT:hi" in via
