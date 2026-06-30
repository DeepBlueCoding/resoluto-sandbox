#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# ///
"""Driver: run 01_local_hello.py INSIDE the local Kata sandbox and print its stdout.

The program is staged into the guest workspace and run with the guest's OWN `python`
on a path relative to that workspace — NOT the host interpreter or host absolute paths,
which do not exist inside the Kata microVM.

Run from resoluto-sandbox/ (local Kata backend provisioned via scripts/local-backend-up.sh):
    uv run python examples/02_run_via_sandbox.py
"""
import io
import os
import sys
from pathlib import Path

from resoluto_sandbox import Sandbox

examples = Path(__file__).resolve().parent
# the local backend needs an image present in its dedicated containerd; override if yours differs
image = os.environ.get("RESOLUTO_LOCAL_LANE_IMAGE", "localhost:5000/resoluto-lane:dev")

result = Sandbox(backend="local", image=image).run(
    ["python", "01_local_hello.py", "sandbox"],   # guest python; path relative to the staged workspace
    workspace=str(examples),
    stream=io.StringIO(),                          # capture only; we print result.output ourselves
)
print(result.output, end="")
sys.exit(result.exit_code)
