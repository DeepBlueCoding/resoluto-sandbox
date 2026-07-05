#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["claude-agent-sdk>=0.1.0"]
# ///
"""A REAL Claude agent for the LLM smoke test.

Reads a prompt from argv, asks Claude (via claude-agent-sdk -> the `claude` CLI), prints the
answer to stdout, and writes result.json {prompt, answer, status} so the host can show a clean
input/output pair. It is a plain program — it never imports resoluto.sandbox.

Auth is the `claude` CLI's job (see docs/auth.md): with a Max/Pro subscription, point
CLAUDE_CONFIG_DIR at a dir holding .credentials.json and do NOT set ANTHROPIC_API_KEY (so usage
bills your subscription, not the API).

    uv run examples/llm_agent.py "In five words, why do sandboxes matter?"
"""
import asyncio
import json
import sys

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    TextBlock,
    query,
)


def _write_result(prompt: str, answer: str, status: str, error: str = "") -> None:
    with open("result.json", "w", encoding="utf-8") as f:
        json.dump({"status": status, "prompt": prompt, "answer": answer, "error": error}, f)


async def main() -> int:
    prompt = " ".join(sys.argv[1:]).strip()
    if not prompt:
        print("usage: llm_agent.py <prompt>", file=sys.stderr)
        return 2

    options = ClaudeAgentOptions(permission_mode="acceptEdits", allowed_tools=[])
    parts: list[str] = []
    result_text = ""
    try:
        async for msg in query(prompt=prompt, options=options):
            if isinstance(msg, AssistantMessage):
                for block in msg.content:
                    if isinstance(block, TextBlock):
                        parts.append(block.text)
            elif isinstance(msg, ResultMessage):
                result_text = msg.result or ""
    except Exception as exc:  # noqa: BLE001 — surface the auth/network cause to the host
        print(
            f"agent error: {exc}\n"
            "A 'Not logged in' / 'error result: success' means the guest has no Claude auth — "
            "stage .credentials.json into CLAUDE_CONFIG_DIR and keep ANTHROPIC_API_KEY unset "
            "(docs/auth.md).",
            file=sys.stderr,
        )
        _write_result(prompt, "", "failure", str(exc))
        return 1

    answer = ("\n".join(parts) if parts else result_text).strip()
    print(answer)
    _write_result(prompt, answer, "success")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
