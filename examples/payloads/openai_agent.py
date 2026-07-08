#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["openai-agents>=0.10.0"]
# ///
"""A minimal OpenAI Agents SDK agent: read a prompt, print the model's answer.

This script is plain — it imports `agents` (the `openai-agents` package), never
`resoluto.sandbox`. It runs identically on your machine and inside the sandbox:

    uv run examples/payloads/openai_agent.py "Say hello in five words"

Needs OPENAI_API_KEY (pay-as-you-go API billing — there is no subscription auth path for
this provider). Model defaults to gpt-4.1-mini; override with OPENAI_MODEL.

Any OpenAI-COMPATIBLE endpoint works by setting OPENAI_BASE_URL (e.g. OpenRouter's
https://openrouter.ai/api/v1) — the SDK is pointed at that host via the Chat Completions
API (what compatible providers implement), and OPENAI_API_KEY carries that host's key.
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

    base_url = os.environ.get("OPENAI_BASE_URL")
    if base_url:
        # OpenAI-compatible endpoint (e.g. OpenRouter): drive it via the Chat Completions API
        # with an explicit client, and disable tracing (its uploads target api.openai.com, which
        # egress locks out). Model is that host's id, e.g. "mistralai/mistral-small-3.2-24b-instruct".
        from agents import OpenAIChatCompletionsModel, set_tracing_disabled
        from openai import AsyncOpenAI

        set_tracing_disabled(True)
        client = AsyncOpenAI(base_url=base_url, api_key=os.environ["OPENAI_API_KEY"])
        model = OpenAIChatCompletionsModel(
            model=os.environ.get("OPENAI_MODEL", "mistralai/mistral-small-3.2-24b-instruct"),
            openai_client=client,
        )
    else:
        model = os.environ.get("OPENAI_MODEL", "gpt-4.1-mini")

    agent = Agent(name="assistant", model=model)
    try:
        result = Runner.run_sync(agent, prompt)
    except Exception as exc:
        print(f"agent error: {exc}", file=sys.stderr)
        return 1

    print(result.final_output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
