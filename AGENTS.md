# AGENTS.md — resoluto-sandbox cheat-sheet

Power-user reference for an LLM coding agent working in this repo.

---

## `Sandbox.run()` contract

```python
from resoluto_sandbox import Sandbox, RunResult

result: RunResult = Sandbox(backend="local").run(
    argv,                   # list[str]: the program + its arguments
    *,
    workspace=None,         # str | None: cwd for the program; default is os.getcwd()
    stdin=None,             # str | bytes | None: fed to stdin
    env=None,               # dict[str, str] | None: overlaid on host env (not replaced)
    output_paths=None,      # Sequence[str] | None: glob patterns to collect as artifacts
    stream=None,            # IO[str] | None: live output sink; None (default) -> sys.stdout; pass a StringIO/file to capture
) -> RunResult
```

`RunResult(exit_code, output, errors, artifacts, result, ok)` — `result` is a parsed
`result.json` if the program wrote one, otherwise `None`; `ok` is `exit_code == 0`.

---

## Program contract (the isolation guarantee)

The program you run is **plain** — it reads `argv` / `stdin`, writes to `stdout` / files, and
exits. It NEVER imports `resoluto_sandbox`. A script that runs as `uv run agent.py` on your
machine runs byte-identically under `Sandbox().run(["uv", "run", "agent.py"])`. This is the
central decoupling guarantee; do not break it.

Dependencies are your program's concern — put `uv run`/`pip install` in your argv, or use a prebuilt image.

---

## CLI commands

```bash
resoluto-sandbox run [--backend local] [--workspace DIR] -- <program> [args...]
resoluto-sandbox doctor
```

`--` separates sandbox options from the program argv. Without `--` or with an empty program the
command exits with code 2.

---

## Footguns

**Local == sandbox decoupling.** `backend="local"` is NOT an isolation boundary — the program
runs as a direct subprocess inheriting the full host environment and filesystem. It is correct,
fast, and useful for development, but do not assume any isolation from it.

**`-e CLAUDE_CODE_OAUTH_TOKEN` with nothing exported = empty auth.** `docker run -e
CLAUDE_CODE_OAUTH_TOKEN` (no `=value`) forwards the host shell's value, which is empty if you
never exported it. The container gets no auth and the CLI returns `Not logged in` (rethrown by
the SDK as the misleading `Claude Code returned an error result: success`). Either `export
CLAUDE_CODE_OAUTH_TOKEN=...` first, or use the `~/.claude/.credentials.json` mount. See
`docs/auth.md`.

**Do NOT set `ANTHROPIC_API_KEY` for subscription billing.** If an API key is present the
`claude` CLI uses it and bills the API instead of your Max/Pro subscription. Leave it unset to
use subscription billing.

**`backend="k8s"` needs an injected `K8sBackend(image=...)`.**  `Sandbox(backend="k8s")` with no
injected backend raises `ValueError` at `run()`. Use `Sandbox(backend=K8sBackend(image="<registry>/resoluto-lane:dev"))`. Also requires a live k3s+Kata cluster, `RESOLUTO_STORE_KIND` set, and `RESOLUTO_SANDBOX_KUBECONTEXT` pinned (fails closed otherwise). `stdin` raises `NotImplementedError` on k8s — deps must be baked into the image. `RunResult.errors` is always empty on k8s; the in-pod runner merges both streams into output.
