---
name: resoluto-sandbox
description: Use when running a program/agent inside the resoluto-sandbox (the `Sandbox.run()` entrypoint), choosing a backend (local or k8s/Kata), wiring dependency strategies (PEP723/requirements/image), the Conduit exchange abstraction, or using the Claude image with a Max subscription. Triggers — "run a script in the sandbox", "Sandbox.run", "resoluto-sandbox run", "bring my own agent", "claude image auth", "swap the backend", "conduit", "deps kind", "task.json wire".
---

# resoluto-sandbox — power-user cheat-sheet

> Deeper docs: [README.md](../../README.md) · [docs/auth.md](../../docs/auth.md) · [docs/concepts.md](../../docs/concepts.md) · [spec/PROTOCOL.md](../../spec/PROTOCOL.md) · [AGENTS.md](../../AGENTS.md)

## Mental model

The sandbox runs a PLAIN program. Your program reads argv/stdin and writes stdout/files/exit code; it never imports `resoluto_sandbox`. Guarantee: a program that runs as `uv run agent.py` on your laptop runs byte-identically under `Sandbox.run()`.

## The one entrypoint

```python
from resoluto_sandbox import Sandbox

out = Sandbox(backend="local").run(
    ["uv", "run", "agent.py", "do the thing"],
    workspace="./proj",
)
out.stdout      # captured output
out.exit_code   # int
out.artifacts   # list[str] of collected file paths for declared output_paths
out.result      # parsed ./result.json if the program wrote it
```

**kwargs:**

| kwarg | Type | Notes |
|-------|------|-------|
| `workspace` | `str \| None` | Directory used as the working dir; default `None` (cwd) |
| `stdin` | `str \| bytes \| None` | Passed to the program's stdin (not supported on `k8s`) |
| `env` | `dict[str, str] \| None` | Extra environment variables |
| `output_paths` | `list[str] \| None` | Glob patterns; matched paths collected into `out.artifacts` |
| `stream` | `IO[str] \| None` | Default `None` echoes to `sys.stdout`; pass a `StringIO`/file to capture |
| `deps` | `Deps \| None` | Dependency resolution strategy (not supported on `k8s`) |

## The two swaps

| Swap | How | Notes |
|------|-----|-------|
| backend | `Sandbox(backend="local")` or `Sandbox(backend=K8sBackend(image=...))` | `local` = subprocess, no setup; `k8s` = Kata pod, needs cluster + store env + kubecontext |
| deps | `Deps(kind="auto"\|"inline"\|"requirements"\|"image"\|"vendored")` | `local` only; bake deps into the image for `k8s` |

`RESOLUTO_STORE_KIND` selects the Conduit store used by the in-pod runner on the k8s path. Proven conduits: `stdout`/`localfs` (local backend) and `s3` (minio/S3-compatible, k8s backend). `gcs` is provided but unverified (experimental).

## The program contract

argv/stdin → your program → stdout (the answer) + files (`output_paths`) + exit code.

Optionally write `./result.json` from inside the program; it is surfaced as `RunResult.result`. The language-neutral wire format for the k8s path is documented in `spec/PROTOCOL.md`.

## Command cheat-sheet

```bash
# CLI run
resoluto-sandbox run --backend local -- uv run agent.py "do the thing"

# Health check
resoluto-sandbox doctor

# Build the Claude image
docker build -f Dockerfile.claude -t resoluto-sandbox:claude .

# Run claude on a Max subscription (mount creds file, not env var)
docker run --rm \
  -v "$HOME/.claude/.credentials.json:/root/.claude/.credentials.json:ro" \
  -v "$PWD/examples:/workspace" \
  resoluto-sandbox:claude \
  python claude_agent.py "Say hi"

# Tests
set -o pipefail && uv run pytest -q                  # unit (fast)
set -o pipefail && uv run pytest -q -m integration   # needs live cluster
```

## Footguns

- **`backend="local"` is a decoupling guarantee, NOT an isolation boundary** — it runs on your host. Hardware isolation is the k8s/Kata path.
- **`-e CLAUDE_CODE_OAUTH_TOKEN` with nothing exported = empty auth** → CLI says "Not logged in", SDK rethrows as the cryptic "error result: success". Mount `~/.claude/.credentials.json` instead, or export the token before passing `-e`.
- **Do NOT set `ANTHROPIC_API_KEY`** if you want Claude Max/Pro subscription billing — it switches the SDK to API-key mode and charges your API account.
- **`backend="k8s"` requires injecting `K8sBackend(image=...)`** — `Sandbox(backend="k8s")` with no image raises `ValueError` at `run()`. Also needs a live k3s+Kata cluster, `RESOLUTO_STORE_KIND` set, and `RESOLUTO_SANDBOX_KUBECONTEXT` pinned. `stdin` and `deps` raise `NotImplementedError` on this backend.
- **No wall-clock timeouts anywhere** — liveness on the k8s path is chunk-arrival + heartbeat, not clock ticks.
- **k8s backend egress is unrestricted by default** — pass `K8sBackend(egress=EgressConfig(...))` (import `EgressConfig` from `resoluto_sandbox.runtime.k8s`) to apply a default-deny NetworkPolicy. See `docs/networking.md`.
- **pytest piped to `tail`/`head` needs `set -o pipefail`** or a failing test suite silently returns exit 0.
