# Credentials: giving your program its secrets

**The sandbox never handles credentials.** It runs your program and forwards whatever you hand it — via `env=`, `env_file=`, `secrets=`, or a mount — into the guest, untouched. It never reads, parses, or knows the format of any provider's credential file. Auth is the *program's* concern: its SDK/CLI resolves the credential from the environment you passed.

## Which credential each provider image needs

Each prebuilt provider image runs a program that talks to that provider's API; supply its credential:

| Provider image                            | Credential env var                                                  | How to obtain                                 |
| ----------------------------------------- | ------------------------------------------------------------------- | --------------------------------------------- |
| `resoluto-sandbox:claude-agent-sdk-<ver>` | `CLAUDE_CODE_OAUTH_TOKEN` (subscription) **or** `ANTHROPIC_API_KEY` | `claude setup-token`, or an Anthropic API key |
| `resoluto-sandbox:langchain-<ver>`        | `ANTHROPIC_API_KEY`                                                 | an Anthropic API key                          |
| `resoluto-sandbox:openai-agents-<ver>`    | `OPENAI_API_KEY`                                                    | an OpenAI API key                             |

## How to pass it

The guest does NOT inherit your host environment — deliver the credential explicitly:

```python
import os
from resoluto.sandbox import Sandbox

# local or k8s — the plain, universal path: forward the env var you already hold
Sandbox(backend="local", image="<provider-image>").run(
    ["python", "openai_agent.py", "Say hi"],
    workspace="examples/payloads",
    env={"OPENAI_API_KEY": os.environ["OPENAI_API_KEY"]},
)
```

- **`env=`** — simplest; the value lands as a literal env entry in the guest (and, on k8s, in the pod spec — prefer `secrets=` there).
- **`secrets=`** — the production path on k8s: `run(secrets={"OPENAI_API_KEY": SecretKeyRef("openai", "key")})` references an existing Kubernetes Secret via `valueFrom.secretKeyRef`, so the value never appears in the pod spec or any log. See [README: Secrets](https://deepbluecoding.github.io/resoluto-sandbox/main/README.md#secrets).
- **`env_file=`** — a dotenv file merged host-side (convenience, not a security boundary).
- **mount** — bind a credential file read-only into the guest (see the subscription note below).

## Claude subscription auth — LOCAL DEV ONLY

> ⚠️ **The Claude Max/Pro subscription path is a local-dev convenience — do NOT use it in cloud or production.** A subscription OAuth token (`CLAUDE_CODE_OAUTH_TOKEN` from `claude setup-token`, or the `~/.claude/.credentials.json` login file) is a **personal, single-user, long-lived** credential: it cannot be scoped or rotated per workload, it bills one human's subscription, and mounting a personal credential file into shared/cloud pods is a credential-leak anti-pattern. For cloud or production, give each workload a **provider API key** delivered via `secrets=` (above).

For local iteration on a Max/Pro plan, forward a token (or mount the login file) and keep `ANTHROPIC_API_KEY` **unset** so the `claude` CLI bills your subscription instead of the API:

```bash
export CLAUDE_CODE_OAUTH_TOKEN=$(claude setup-token)   # long-lived token; keep ANTHROPIC_API_KEY unset
uv run python examples/run_agent_in_sandbox.py claude "Say hello in five words"
```

Or mount the single login file read-only (not the whole `~/.claude` dir — the CLI writes cache into its config dir and would fail against a read-only mount of the entire directory):

```bash
docker run --rm \
  -v "$HOME/.claude/.credentials.json:/root/.claude/.credentials.json:ro" \
  -v "$PWD/examples/payloads:/workspace" \
  resoluto-sandbox:claude-agent-sdk-<ver> python claude_agent.py "Say hello in five words"
```

### Gotcha: `-e CLAUDE_CODE_OAUTH_TOKEN` with nothing exported

`docker run -e CLAUDE_CODE_OAUTH_TOKEN` (no `=value`) forwards the host's value — empty if you never exported it. The container then has no auth and the CLI returns `Not logged in`, which the SDK rethrows as the confusing `Claude Code returned an error result: success`. Either `export` the token first, or use the credentials-file mount above.
