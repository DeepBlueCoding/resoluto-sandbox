"""Verify that a plain program produces identical output when run directly and via Sandbox."""
import subprocess
import sys

from resoluto_sandbox import Sandbox


def test_same_program_local_and_direct(tmp_path):
    script = tmp_path / "echo_prompt.py"
    script.write_text("import sys; print('OUT:' + (sys.argv[1] if len(sys.argv) > 1 else ''))")
    direct = subprocess.run([sys.executable, str(script), "hi"], capture_output=True, text=True).stdout
    via = Sandbox(backend="local").run([sys.executable, str(script), "hi"], workspace=str(tmp_path)).stdout
    assert direct.strip() == via.strip() == "OUT:hi"
