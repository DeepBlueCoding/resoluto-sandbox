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

  name        payload              credential env           LLM host           notes
  ----------  -------------------  -----------------------  -----------------  ------------------------
  claude      claude_agent.py      CLAUDE_CODE_OAUTH_TOKEN  api.anthropic.com
  langchain   langchain_agent.py   ANTHROPIC_API_KEY        api.anthropic.com
  openai      openai_agent.py      OPENAI_API_KEY           api.openai.com
  openrouter  openai_agent.py      OPENROUTER_API_KEY       openrouter.ai      OpenAI-compatible: reuses
                                                                               the openai image + payload
"""

import io
import os
import shutil
import sys
import tempfile
from importlib.metadata import version as _pkg_version
from pathlib import Path

from resoluto.sandbox import Sandbox
from resoluto.sandbox.images import image_tags, pullable

PAYLOADS = Path(__file__).resolve().parent / "payloads"
DEFAULT_PROMPT = "In five words, why run an untrusted agent in a sandbox?"

# provider -> how to run it:
#   payload    the guest program (a plain script; never imports resoluto.sandbox)
#   image      which image_tags entry to boot (openrouter REUSES openai's — it's OpenAI-compatible)
#   auth_env   the HOST env var carrying this provider's credential
#   auth_as    the env var the guest reads it under (defaults to auth_env; openrouter forwards its
#              key as OPENAI_API_KEY so the shared openai payload consumes it unchanged)
#   host       the single LLM endpoint egress is locked to
#   guest_env  any extra env the payload needs (openrouter: the base_url + a valid openrouter model)
PROVIDERS = {
    "claude": {
        "payload": "claude_agent.py",
        "image": "claude",
        "auth_env": "CLAUDE_CODE_OAUTH_TOKEN",
        "host": "api.anthropic.com",
    },
    "langchain": {
        "payload": "langchain_agent.py",
        "image": "langchain",
        "auth_env": "ANTHROPIC_API_KEY",
        "host": "api.anthropic.com",
    },
    "openai": {
        "payload": "openai_agent.py",
        "image": "openai",
        "auth_env": "OPENAI_API_KEY",
        "host": "api.openai.com",
    },
    "openrouter": {
        "payload": "openai_agent.py",
        "image": "openai",
        "auth_env": "OPENROUTER_API_KEY",
        "auth_as": "OPENAI_API_KEY",
        "host": "openrouter.ai",
        "guest_env": {
            "OPENAI_BASE_URL": "https://openrouter.ai/api/v1",
            "OPENAI_MODEL": os.environ.get(
                "OPENROUTER_MODEL", "mistralai/mistral-small-3.2-24b-instruct"
            ),
        },
    },
}


def main() -> int:
    args = sys.argv[1:]
    if not args or args[0] not in PROVIDERS:
        print(f"usage: run_agent_in_sandbox.py <{'|'.join(PROVIDERS)}> [prompt]", file=sys.stderr)
        return 2
    provider = args[0]
    prompt = " ".join(args[1:]).strip() or DEFAULT_PROMPT
    spec = PROVIDERS[provider]
    payload, llm_host = spec["payload"], spec["host"]

    # the registry-qualified reference (localhost:5000/…) the local backend pulls; `image build`
    # pushed it there, so no manual load step is needed.
    image = pullable(image_tags(_pkg_version("resoluto-sandbox"))[spec["image"]])
    token = os.environ.get(spec["auth_env"])
    if not token:
        print(
            f"set {spec['auth_env']} (the {provider} provider's own credential) — the sandbox "
            f"forwards it to the guest via env=.",
            file=sys.stderr,
        )
        return 1

    # forward the secret under the name the guest expects, plus any provider-specific config
    guest_env = {spec.get("auth_as", spec["auth_env"]): token, **spec.get("guest_env", {})}

    # a throwaway workspace holding just the agent program — the credential travels via env, not files
    with tempfile.TemporaryDirectory(prefix="agent-ws-") as ws:
        shutil.copy(PAYLOADS / payload, Path(ws) / payload)
        result = Sandbox(backend="local", image=image).run(
            ["python", payload, prompt],
            workspace=ws,
            env=guest_env,  # forward the provider's secret; the sandbox never inspects it
            egress=[llm_host],  # lock egress to this provider's LLM endpoint only
            stream=io.StringIO(),
        )
    print(f"\nprovider : {provider}  ({image})")
    print(f"INPUT    : {prompt!r}")
    print(f"OUTPUT   : {result.output.strip()!r}")
    print(f"exit={result.exit_code}  — ran isolated in a Kata microVM, egress={llm_host} only")
    return result.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
