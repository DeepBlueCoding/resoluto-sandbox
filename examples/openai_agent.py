#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["openai-agents>=0.10.0"]
# ///
"""A minimal OpenAI Agents SDK agent: read a prompt, print the model's answer.

This script is plain — it imports `agents` (the `openai-agents` package), never
`resoluto.sandbox`. It runs identically on your machine and inside the sandbox:

    uv run examples/openai_agent.py "Say hello in five words"

Needs OPENAI_API_KEY (pay-as-you-go API billing — there is no subscription auth path for
this provider). Model defaults to gpt-4.1-mini; override with OPENAI_MODEL.
"""
import os
import sys

from agents import Agent, Runner


def main() -> int:
    prompt = " ".join(sys.argv[1:]).strip() or sys.stdin.read().strip()
    if not prompt:
        print("usage: openai_agent.py <prompt>", file=sys.stderr)
        return 2
    if not os.environ.get("OPENAI_API_KEY"):
        print(
            "agent error: OPENAI_API_KEY is not set. openai-agents calls the OpenAI API "
            "directly — pass it via env= on Sandbox.run(), or bake it into the image as a secret.",
            file=sys.stderr,
        )
        return 1

    agent = Agent(name="assistant", model=os.environ.get("OPENAI_MODEL", "gpt-4.1-mini"))
    try:
        result = Runner.run_sync(agent, prompt)
    except Exception as exc:
        print(f"agent error: {exc}", file=sys.stderr)
        return 1

    print(result.final_output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
