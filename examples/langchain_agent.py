#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["langchain-anthropic>=0.3.0"]
# ///
"""A minimal LangChain agent: read a prompt, print Claude's answer via langchain-anthropic.

This script is plain — it imports `langchain_anthropic`, never `resoluto_sandbox`. It runs
identically on your machine and inside the sandbox:

    uv run examples/langchain_agent.py "Say hello in five words"

Unlike the `claude` provider (examples/claude_agent.py), langchain-anthropic calls the
Anthropic API directly — there is no Max/Pro subscription path here. It needs
ANTHROPIC_API_KEY. Model defaults to claude-sonnet-4-5; override with ANTHROPIC_MODEL.
"""
import os
import sys

from langchain_anthropic import ChatAnthropic


def main() -> int:
    prompt = " ".join(sys.argv[1:]).strip() or sys.stdin.read().strip()
    if not prompt:
        print("usage: langchain_agent.py <prompt>", file=sys.stderr)
        return 2
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print(
            "agent error: ANTHROPIC_API_KEY is not set. langchain-anthropic calls the "
            "Anthropic API directly (no subscription/CLI auth path) — pass it via env= "
            "on Sandbox.run(), or bake it into the image as a secret.",
            file=sys.stderr,
        )
        return 1

    model = ChatAnthropic(model=os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-5"))
    try:
        answer = model.invoke(prompt).content
    except Exception as exc:
        print(f"agent error: {exc}", file=sys.stderr)
        return 1

    print(answer if isinstance(answer, str) else str(answer))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
