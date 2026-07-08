#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# ///
"""Run an untrusted program isolated in a Kata microVM — here, a Claude agent as the sample workload.

The sandbox itself knows nothing about agents; it runs whatever program you hand it, isolated.
`payloads/claude_agent.py` is just such a program: a PLAIN script that makes a live LLM call via
claude-agent-sdk and never imports `resoluto.sandbox`. This driver wraps it from the OUTSIDE — runs it
hardware-isolated in a Kata microVM (local backend), with network egress locked to one endpoint, and
its input/output round-tripped through the store. Swap the payload for any other program to run THAT
isolated instead — that is all the sandbox does.

Prereqs — from resoluto-sandbox/, local Kata backend provisioned:
    bash scripts/local-backend-up.sh                 # provision the local Kata backend
    set -a; source local.env; set +a                 # exports RESOLUTO_SANDBOX_IMAGE (the sandbox image)
    claude setup-token                               # or `claude` login -> ~/.claude/.credentials.json

    uv run python examples/run_agent_in_sandbox.py "In five words, why isolate an agent?"

Auth (docs/auth.md): the guest `claude` CLI authenticates with CLAUDE_CODE_OAUTH_TOKEN (your
subscription token); ANTHROPIC_API_KEY stays UNSET so usage bills your subscription, not the API.
"""
import io
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

from resoluto.sandbox import Sandbox

PAYLOAD = Path(__file__).resolve().parent / "payloads" / "claude_agent.py"
CREDS = Path.home() / ".claude" / ".credentials.json"
DEFAULT_PROMPT = "In five words, why run an untrusted agent in a sandbox?"


def _oauth_token() -> str | None:
    """The Claude OAuth token: env first, else the subscription access token from the creds file."""
    tok = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")
    if tok:
        return tok
    if CREDS.exists():
        try:
            return json.loads(CREDS.read_text())["claudeAiOauth"]["accessToken"]
        except (KeyError, ValueError):
            return None
    return None


def main() -> int:
    prompt = " ".join(sys.argv[1:]).strip() or DEFAULT_PROMPT

    image = os.environ.get("RESOLUTO_SANDBOX_IMAGE")
    if not image:
        print("set RESOLUTO_SANDBOX_IMAGE first (the provisioned sandbox image):  "
              "set -a; source local.env; set +a", file=sys.stderr)
        return 1
    token = _oauth_token()
    if not token:
        print("no Claude auth — run `claude setup-token` (or log in with `claude`). "
              "ANTHROPIC_API_KEY is not used.", file=sys.stderr)
        return 1

    # a throwaway workspace holding just the agent program — the token travels via env, not files
    with tempfile.TemporaryDirectory(prefix="agent-ws-") as ws:
        shutil.copy(PAYLOAD, Path(ws) / "claude_agent.py")
        result = Sandbox(backend="local", image=image).run(
            ["python", "claude_agent.py", prompt],
            workspace=ws,
            env={"CLAUDE_CODE_OAUTH_TOKEN": token},   # authenticate the guest claude CLI
            egress=["api.anthropic.com"],             # local backend: lock egress to the LLM only (SNI proxy)
            stream=io.StringIO(),                     # capture substrate telemetry; print the clean answer below
        )
    print(f"\nINPUT  (prompt) : {prompt!r}")
    print(f"OUTPUT (answer) : {result.output.strip()!r}")
    print(f"exit={result.exit_code}  — ran in a Kata microVM, egress=api.anthropic.com only")
    return result.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
