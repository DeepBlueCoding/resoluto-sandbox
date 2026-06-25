# resoluto-sandbox

Run any program or agent script in a sandbox. Your program stays plain — it imports your own SDK,
never `resoluto_sandbox`. The same script that runs with `uv run agent.py` on your machine runs
unchanged inside the sandbox.

<p align="left">
  <img alt="python" src="https://img.shields.io/badge/python-3.12%2B-blue">
  <img alt="status" src="https://img.shields.io/badge/status-alpha-orange">
</p>

---

## Install

```bash
pip install resoluto-sandbox   # published wheel coming; for now: pip install -e .
```

---

## 60-second local quickstart

No cloud account, no Docker, no config. Run a program as a subprocess and capture its output:

```python
from resoluto_sandbox import Sandbox

result = Sandbox(backend="local").run(
    ["python", "-c", "print('hello from the sandbox')"]
)
print(result.stdout)   # hello from the sandbox
print(result.ok)       # True
```

`backend="local"` runs the program as a subprocess on this host. The result captures stdout,
stderr, the exit code, and any files you asked to collect.

---

## Run your own agent, unchanged

The sandbox enforces one contract: **your program never imports `resoluto_sandbox`**. It reads
`argv`/`stdin` and writes to `stdout`/files and exits. That is the whole interface.

The same invocation that works directly also works inside the sandbox:

```bash
uv run examples/claude_agent.py "Summarise the Zen of Python"
```

```python
from resoluto_sandbox import Sandbox

result = Sandbox(backend="local").run(
    ["uv", "run", "examples/claude_agent.py", "Summarise the Zen of Python"]
)
print(result.stdout)
```

See `examples/claude_agent.py` for a minimal Claude agent and `docs/auth.md` for the
Claude Max/Pro subscription auth path (no API key needed).

---

## Dependencies

`Deps(kind=...)` controls how the program's dependencies are supplied:

| kind | behaviour |
|---|---|
| `auto` (default) | detects PEP 723 inline script / `requirements.txt` / `pyproject.toml` automatically |
| `inline` | wraps the call with `uv run` to install inline deps |
| `requirements` | passes `--with-requirements requirements.txt` to `uv run` |
| `image` | the program is already installed in the image — pass argv through as-is |
| `vendored` | same as `image`; use when deps are vendored in the workspace |

```python
from resoluto_sandbox import Sandbox, Deps

result = Sandbox().run(["agent.py", "--prompt", "hello"], deps=Deps(kind="inline"))
```

---

## CLI

```bash
resoluto-sandbox run -- echo hi          # run any command via the local backend
resoluto-sandbox run -- python agent.py  # any program; -- separates sandbox opts from argv
resoluto-sandbox doctor                  # check what is available on this machine
```

---

## `Sandbox.run()` reference

```python
Sandbox(backend="local").run(
    argv,                        # program + arguments
    *,
    workspace=None,              # working directory for the program (default: cwd)
    stdin=None,                  # str or bytes fed to stdin
    env=None,                    # dict overlaid on the host environment
    output_paths=None,           # list of glob patterns to collect as artifacts
    stream=None,                 # live stdout sink; None (default) echoes to sys.stdout; pass a StringIO/file to capture
    deps=None,                   # Deps() strategy; default is Deps(kind="auto")
) -> RunResult
```

`RunResult` fields: `exit_code`, `stdout`, `stderr`, `artifacts`, `result` (parsed `result.json`
if the program wrote one), `ok` (property: `exit_code == 0`).

---

## Status

| Feature | Status |
|---|---|
| `backend="local"` — subprocess on host, full env inheritance, live stdout | **works today** |
| `Deps` strategies: `auto`, `inline`, `requirements`, `image`, `vendored` | **works today** |
| CLI: `run` + `doctor` | **works today** |
| `Conduit` abstraction + `LocalConduit`, `StdoutConduit`, `S3Conduit`, `GcsConduit` | **works today** |
| Language-neutral wire spec | **published** — see `spec/PROTOCOL.md` |
| `backend="k8s"` — Kata microVM isolation | design / roadmap (raises `NotImplementedError` today) |
| Prebuilt image matrix (`-base`, `-runner`, langchain, openai variants) + `image build` CLI | design / roadmap |
| Worker migration utilities | design / roadmap |

---

## Further reading

- `docs/auth.md` — Claude Max/Pro subscription auth (local and container)
- `spec/PROTOCOL.md` — language-neutral host ↔ sandbox wire protocol (JSON Schemas included)
- `examples/` — runnable examples ladder:
  - `uv run examples/01_local_hello.py` — standalone program, no sandbox
  - `uv run python examples/02_run_via_sandbox.py` — same program via `Sandbox.run()`
  - `examples/claude_agent.py` — BYO Claude agent (plain script, no `resoluto_sandbox` import)
