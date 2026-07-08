#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# ///
"""Run a provider's agent isolated in a Kata microVM — symmetric across every image the sandbox ships.

The sandbox is provider-agnostic: it stages a program and forwards whatever secret you pass via `env=`.
Nothing here privileges one provider — you pick a name and the driver looks up the matching prebuilt
image (via the sandbox's own `image_tags()`), the payload program, the credential env var, and the LLM
host to allow. `payloads/<name>_agent.py` is a PLAIN program; it never imports `resoluto.sandbox`.

    resoluto-sandbox image build --provider <name>        # build the overlay image (claude|langchain|openai)
    # transfer it into the local containerd (README: Prebuilt provider images)
    export <AUTH_ENV>=...                                  # the provider's OWN credential (table below)
    uv run python examples/run_agent_in_sandbox.py <name> "your prompt"

  name       payload              credential env            LLM host
  ---------  -------------------  ------------------------  -----------------
  claude     claude_agent.py      CLAUDE_CODE_OAUTH_TOKEN   api.anthropic.com
  langchain  langchain_agent.py   ANTHROPIC_API_KEY         api.anthropic.com
  openai     openai_agent.py      OPENAI_API_KEY            api.openai.com
"""
import io
import os
import shutil
import sys
import tempfile
from importlib.metadata import version as _pkg_version
from pathlib import Path

from resoluto.sandbox import Sandbox
from resoluto.sandbox.images import image_tags

PAYLOADS = Path(__file__).resolve().parent / "payloads"
DEFAULT_PROMPT = "In five words, why run an untrusted agent in a sandbox?"

# provider -> (payload program, the env var carrying ITS credential, the LLM host to allow egress to)
PROVIDERS = {
    "claude":    ("claude_agent.py",    "CLAUDE_CODE_OAUTH_TOKEN", "api.anthropic.com"),
    "langchain": ("langchain_agent.py", "ANTHROPIC_API_KEY",       "api.anthropic.com"),
    "openai":    ("openai_agent.py",    "OPENAI_API_KEY",          "api.openai.com"),
}


def main() -> int:
    args = sys.argv[1:]
    if not args or args[0] not in PROVIDERS:
        print(f"usage: run_agent_in_sandbox.py <{'|'.join(PROVIDERS)}> [prompt]", file=sys.stderr)
        return 2
    provider = args[0]
    prompt = " ".join(args[1:]).strip() or DEFAULT_PROMPT
    payload, auth_env, llm_host = PROVIDERS[provider]

    image = image_tags(_pkg_version("resoluto-sandbox"))[provider]
    token = os.environ.get(auth_env)
    if not token:
        print(f"set {auth_env} (the {provider} provider's own credential) — the sandbox forwards it "
              f"to the guest via env=.", file=sys.stderr)
        return 1

    # a throwaway workspace holding just the agent program — the credential travels via env, not files
    with tempfile.TemporaryDirectory(prefix="agent-ws-") as ws:
        shutil.copy(PAYLOADS / payload, Path(ws) / payload)
        result = Sandbox(backend="local", image=image).run(
            ["python", payload, prompt],
            workspace=ws,
            env={auth_env: token},        # forward the provider's secret; the sandbox never inspects it
            egress=[llm_host],            # lock egress to this provider's LLM endpoint only
            stream=io.StringIO(),
        )
    print(f"\nprovider : {provider}  ({image})")
    print(f"INPUT    : {prompt!r}")
    print(f"OUTPUT   : {result.output.strip()!r}")
    print(f"exit={result.exit_code}  — ran isolated in a Kata microVM, egress={llm_host} only")
    return result.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
