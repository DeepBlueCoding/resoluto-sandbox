#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# ///
"""Driver: run 01_local_hello.py via Sandbox.run() and print captured stdout.

Run from the repo root:
    uv run python examples/02_run_via_sandbox.py
"""
import io
import sys
from pathlib import Path

from resoluto_sandbox import Sandbox

repo_root = Path(__file__).resolve().parent.parent
hello_script = repo_root / "examples" / "01_local_hello.py"

sink = io.StringIO()
result = Sandbox(backend="local").run(
    [sys.executable, str(hello_script), "sandbox"],
    workspace=str(repo_root),
    stream=sink,
)
print(result.stdout, end="")
sys.exit(result.exit_code)
