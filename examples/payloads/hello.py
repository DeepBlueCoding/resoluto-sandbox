#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# ///
"""A minimal standalone program that prints a greeting.

Run directly:
    uv run examples/payloads/hello.py

Run via Sandbox (from resoluto-sandbox/):
    uv run python examples/run_hello_in_sandbox.py
"""

import sys

name = sys.argv[1] if len(sys.argv) > 1 else "world"
print(f"Hello, {name}!")
