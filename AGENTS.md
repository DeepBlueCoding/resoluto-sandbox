# AGENTS.md — resoluto-sandbox cheat-sheet

Power-user reference for an LLM coding agent working in this repo.

---

## `Sandbox.run()` contract

```python
from resoluto_sandbox import Sandbox, Deps, RunResult

result: RunResult = Sandbox(backend="local").run(
    argv,                   # list[str]: the program + its arguments
    *,
    workspace=None,         # str | None: cwd for the program; default is os.getcwd()
    stdin=None,             # str | bytes | None: fed to stdin
    env=None,               # dict[str, str] | None: overlaid on host env (not replaced)
    output_paths=None,      # Sequence[str] | None: glob patterns to collect as artifacts
    stream=None,            # IO[str] | None: live stdout sink; None (default) -> sys.stdout; pass a StringIO/file to capture
    deps=None,              # Deps | None: dependency strategy; default Deps(kind="auto")
) -> RunResult
```

`RunResult(exit_code, stdout, stderr, artifacts, result, ok)` — `result` is a parsed
`result.json` if the program wrote one, otherwise `None`; `ok` is `exit_code == 0`.

---

## Program contract (the isolation guarantee)

The program you run is **plain** — it reads `argv` / `stdin`, writes to `stdout` / files, and
exits. It NEVER imports `resoluto_sandbox`. A script that runs as `uv run agent.py` on your
machine runs byte-identically under `Sandbox().run(["uv", "run", "agent.py"])`. This is the
central decoupling guarantee; do not break it.

---

## `Deps` kinds

| kind | what happens |
|---|---|
| `auto` | detect PEP 723 inline script → `uv run`; `requirements.txt` → `uv run --with-requirements`; `pyproject.toml` → `uv run`; otherwise pass argv through |
| `inline` | always wrap with `uv run` (handles PEP 723 inline deps) |
| `requirements` | `uv run --with-requirements <workspace>/requirements.txt` |
| `image` | pass argv through unchanged (deps already in the image or on PATH) |
| `vendored` | same as `image` |

---

## CLI commands

```bash
resoluto-sandbox run [--backend local] [--workspace DIR] [--deps-kind KIND] -- <program> [args...]
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

**`backend="k8s"` raises `NotImplementedError`.** The k8s backend is not wired in this build.
Use `backend="local"` (the default). See the Status table in README.md for the roadmap.
