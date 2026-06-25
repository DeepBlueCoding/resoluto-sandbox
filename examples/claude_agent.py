#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["claude-agent-sdk>=0.1.0"]
# ///
"""A minimal Claude agent: read a prompt, print Claude's answer.

This script is plain — it imports `claude_agent_sdk`, never `resoluto_sandbox`.
It runs identically on your machine and inside the sandbox:

    uv run examples/claude_agent.py "Say hello in five words"          # local
    python -c "from resoluto_sandbox import Sandbox; \
        print(Sandbox().run(['uv','run','examples/claude_agent.py','Say hi']).stdout)"

Auth is handled entirely by the `claude` CLI the SDK forks — see docs/auth.md.
With a Claude Max/Pro subscription, log in once (`claude` / `claude setup-token`)
and do NOT set ANTHROPIC_API_KEY, so usage bills your subscription, not the API.
"""
import asyncio
import sys

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    TextBlock,
    query,
)


async def main() -> int:
    prompt = " ".join(sys.argv[1:]).strip() or sys.stdin.read().strip()
    if not prompt:
        print("usage: claude_agent.py <prompt>", file=sys.stderr)
        return 2

    options = ClaudeAgentOptions(permission_mode="acceptEdits", allowed_tools=[])
    text_parts: list[str] = []
    result_text = ""
    try:
        async for msg in query(prompt=prompt, options=options):
            if isinstance(msg, AssistantMessage):
                for block in msg.content:
                    if isinstance(block, TextBlock):
                        text_parts.append(block.text)
            elif isinstance(msg, ResultMessage):
                result_text = msg.result or ""
    except Exception as exc:
        print(
            f"agent error: {exc}\n"
            "A 'Not logged in' / 'error result: success' here means the sandbox has "
            "no Claude auth. Pass CLAUDE_CODE_OAUTH_TOKEN or mount "
            "~/.claude/.credentials.json, and do NOT set ANTHROPIC_API_KEY if you want "
            "subscription billing. See docs/auth.md.",
            file=sys.stderr,
        )
        return 1

    print("\n".join(text_parts) if text_parts else result_text)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
