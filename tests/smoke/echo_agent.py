#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# ///
"""A minimal deterministic 'agent' for the smoke test.

It follows the documented program contract exactly (see the resoluto-sandbox skill,
references/agents.md): it receives its task on argv (and an optional SMOKE_TAG via env),
'reasons' over the input, prints the answer to stdout, and writes result.json. It NEVER
imports resoluto.sandbox — it is a plain program, identical on your host and in the sandbox.

    uv run tests/smoke/echo_agent.py "ping-42"
"""
import json
import os
import sys

prompt = " ".join(sys.argv[1:]).strip()
if not prompt:
    print("usage: echo_agent.py <prompt>", file=sys.stderr)
    raise SystemExit(2)

answer = f"ECHO: {prompt[::-1]}"          # deterministic 'reasoning' over the argv input
print(answer)                              # stdout -> RunResult.output

tag = os.environ.get("SMOKE_TAG")
if tag:
    print(f"TAG: {tag}")                   # proves env= reached the guest

# optional typed verdict -> RunResult.result (collected via output_paths=["result.json"])
with open("result.json", "w", encoding="utf-8") as f:
    json.dump({"status": "success", "node_id": "echo", "exit_code": 0}, f)
