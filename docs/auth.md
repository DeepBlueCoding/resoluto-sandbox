# Auth: using your Claude Max/Pro subscription

The sandbox never handles credentials. It runs your program; the `claude` CLI
that the Claude Agent SDK forks resolves auth on its own, from any of these
(in the SDK's order of preference):

1. `CLAUDE_CODE_OAUTH_TOKEN` — a long-lived OAuth token
2. `~/.claude/.credentials.json` — the subscription login file (under `$HOME`,
   or `$CLAUDE_CONFIG_DIR` if set)
3. `ANTHROPIC_API_KEY` — pay-as-you-go API billing

> **To bill your Max/Pro subscription, use option 1 or 2 and make sure
> `ANTHROPIC_API_KEY` is NOT set.** If an API key is present the CLI uses it and
> bills the API instead of your subscription.

## Local backend (simplest)

`Sandbox(backend="local")` runs your program as a subprocess that **inherits your
host environment**. So if you are already logged in to Claude Code on this
machine, it just works — nothing to configure:

```bash
claude            # one-time interactive login on your Max/Pro account (if needed)

python -c "from resoluto_sandbox import Sandbox; \
  print(Sandbox().run(['uv','run','examples/claude_agent.py','Say hello in five words']).output)"
```

The subprocess sees your `~/.claude/.credentials.json`, authenticates with your
subscription, and prints Claude's answer.

## Container image

The image bakes the CLI + SDK but no credentials. Supply auth at `docker run`:

**A long-lived token (best for containers / CI):**

```bash
claude setup-token                       # prints an OAuth token; copy it
export CLAUDE_CODE_OAUTH_TOKEN=...        # the value from above

docker run --rm -e CLAUDE_CODE_OAUTH_TOKEN \
  -v "$PWD/examples:/workspace" \
  resoluto-sandbox:claude python claude_agent.py "Say hello in five words"
```

**Or mount just your subscription login file (read-only):**

```bash
docker run --rm \
  -v "$HOME/.claude/.credentials.json:/root/.claude/.credentials.json:ro" \
  -v "$PWD/examples:/workspace" \
  resoluto-sandbox:claude python claude_agent.py "Say hello in five words"
```

Mount the single `.credentials.json` file, not the whole `~/.claude` directory —
the CLI writes history/cache into its config dir and would fail against a
read-only mount of the entire directory.

In both cases `ANTHROPIC_API_KEY` is intentionally absent, so usage bills your
subscription.

## Gotcha: `-e CLAUDE_CODE_OAUTH_TOKEN` with nothing exported

`docker run -e CLAUDE_CODE_OAUTH_TOKEN` (no `=value`) forwards the host's value —
which is empty if you never exported it. The container then has no auth and the
CLI returns `Not logged in`, which the SDK rethrows as the confusing
`Claude Code returned an error result: success`. Either `export` the token first,
or use the credentials-file mount above.
