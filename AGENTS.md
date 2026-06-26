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
    stdin=None,             # NOT SUPPORTED — raises NotImplementedError on both backends
    env=None,               # dict[str, str] | None: overlaid on sandbox env (not replaced)
    output_paths=None,      # Sequence[str] | None: glob patterns to collect as artifacts
    stream=None,            # IO[str] | None: live output sink; None (default) -> sys.stdout; pass a StringIO/file to capture
) -> RunResult
```

`RunResult(exit_code, output, errors, artifacts, result, ok)` — `result` is a parsed
`result.json` if the program wrote one, otherwise `None`; `ok` is `exit_code == 0`.

---

## Program contract (the isolation guarantee)

The program you run is **plain** — it reads `argv`, writes to `stdout` / files, and
exits. It NEVER imports `resoluto_sandbox`. A script that works as `uv run agent.py` on your
machine works inside the sandbox too; `backend="local"` runs it in a Docker container with
OS-level isolation, `backend="k8s"` runs it in a Kata microVM. The backend changes only where
it runs, not what runs.

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

**Local uses Docker, needs an image.** `backend="local"` runs the program in a Docker container
(OS-level isolation: separate PID/mount/network namespaces, cgroups). It requires Docker to be
running and an image that contains python + the resoluto-sandbox wheel + your program's deps
(default `resoluto-sandbox-runner:dev`; override with `image=`). The egress canary is skipped
(`RESOLUTO_TRUSTED_LOCAL=1` is set by the local preset), so local is NOT egress-locked — use
`backend="k8s"` for locked-down egress or hardware isolation.

**`-e CLAUDE_CODE_OAUTH_TOKEN` with nothing exported = empty auth.** `docker run -e
CLAUDE_CODE_OAUTH_TOKEN` (no `=value`) forwards the host shell's value, which is empty if you
never exported it. The container gets no auth and the CLI returns `Not logged in` (rethrown by
the SDK as the misleading `Claude Code returned an error result: success`). Either `export
CLAUDE_CODE_OAUTH_TOKEN=...` first, or use the `~/.claude/.credentials.json` mount. See
`docs/auth.md`.

**Do NOT set `ANTHROPIC_API_KEY` for subscription billing.** If an API key is present the
`claude` CLI uses it and bills the API instead of your Max/Pro subscription. Leave it unset to
use subscription billing.

**`backend="k8s"` needs a `SubstrateBackend` injection or sets `RESOLUTO_LANE_IMAGE`.**
`Sandbox(backend="k8s")` without an image raises `ValueError` at `run()`. Inject a configured
`SubstrateBackend` or set `RESOLUTO_LANE_IMAGE`. Also requires a Kubernetes cluster (k3s, kind,
EKS, or any distribution) with Kata Containers, `RESOLUTO_STORE_KIND` set, and
`RESOLUTO_SANDBOX_KUBECONTEXT` pinned (fails closed otherwise). `stdin` raises
`NotImplementedError` on both backends — deps must be baked into the image. `RunResult.errors`
is always empty; the in-sandbox runner merges both streams into output.
