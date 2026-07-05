#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["langchain-anthropic>=0.3.0"]
# ///
"""A minimal LangChain agent using the Anthropic integration: read a prompt, print Claude's answer.

This script is plain — it imports `langchain_anthropic`, never `resoluto.sandbox`. It runs
identically on your machine and inside the sandbox — via `uv run`:

    uv run examples/langchain_agent.py "Say hello in five words"

IMPORTANT: the prebuilt `resoluto-sandbox:langchain-<ver>` image ships ONLY bare `langchain` +
`langgraph` — LangChain itself is provider-agnostic and does NOT bundle any LLM integration.
`langchain-anthropic` (which this script imports) is NOT in that image. Running this script
INSIDE the plain prebuilt image will fail with ImportError. To run it in the sandbox, extend the
Dockerfile with one line:

    FROM resoluto-sandbox:langchain-<ver>
    RUN pip install --break-system-packages langchain-anthropic

(swap for `langchain-openai`, `langchain-google-genai`, etc. to target a different provider —
same pattern, different package and Chat* class). `uv run` on your host works unconditionally
since the script's own dependency header pulls in `langchain-anthropic` directly.

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
