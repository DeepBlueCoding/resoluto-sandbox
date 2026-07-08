#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# ///
"""Driver: run payloads/hello.py INSIDE the local Kata sandbox and print its stdout — the bare mechanics.

The program is staged into the guest workspace and run with the guest's OWN `python` on a path
relative to that workspace — NOT the host interpreter or host absolute paths, which do not exist
inside the Kata microVM. `payloads/hello.py` is a plain program; it never imports resoluto.sandbox.

Run from resoluto-sandbox/ (local Kata backend provisioned via scripts/local-backend-up.sh):
    set -a; source local.env; set +a          # exports RESOLUTO_SANDBOX_IMAGE
    uv run python examples/run_hello_in_sandbox.py
"""
import io
import os
import sys
from pathlib import Path

from resoluto.sandbox import Sandbox

payloads = Path(__file__).resolve().parent / "payloads"
image = os.environ.get("RESOLUTO_SANDBOX_IMAGE")
if not image:
    sys.exit("set RESOLUTO_SANDBOX_IMAGE first (the provisioned sandbox image):  set -a; source local.env; set +a")

result = Sandbox(backend="local", image=image).run(
    ["python", "hello.py", "sandbox"],   # guest python; path relative to the staged workspace
    workspace=str(payloads),
    stream=io.StringIO(),                # capture only; we print result.output ourselves
)
print(result.output, end="")
sys.exit(result.exit_code)
