#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# ///
"""A minimal standalone program that prints a greeting.

Run directly:
    uv run examples/01_local_hello.py

Run via Sandbox (from the repo root):
    uv run python examples/02_run_via_sandbox.py
"""
import sys

name = sys.argv[1] if len(sys.argv) > 1 else "world"
print(f"Hello, {name}!")
