# Examples

A minimal end to end: write a plain program, stage it into a sandbox, and drive it with [`Sandbox.run`](https://deepbluecoding.github.io/resoluto-sandbox/main/api/sandbox/#resoluto.sandbox.Sandbox). The program is ordinary — it never imports `resoluto.sandbox`. That is the whole point: a program that runs as `python hello.py` on your host runs unchanged inside the guest.

## 1. The program (the cargo)

A payload is a plain script. It reads its inputs from `argv` / `env` and writes its outputs to stdout or to files in its working directory.

```python
# hello.py
import os
import sys

who = sys.argv[1] if len(sys.argv) > 1 else "world"
greeting = os.environ.get("GREETING", "hello")
print(f"{greeting}, {who}")
```

## 2. Drive it

`Sandbox(backend=...)` picks the isolation; `.run(argv, ...)` stages the workspace, runs the program on the **guest's** interpreter, and returns a [`RunResult`](https://deepbluecoding.github.io/resoluto-sandbox/main/api/sandbox/#resoluto.sandbox.RunResult). Paths in `argv` are relative to the staged `workspace`, never host absolute paths.

```python
from resoluto.sandbox import Sandbox

result = Sandbox(backend="local").run(
    ["python", "hello.py", "sandbox"],   # guest python; path relative to the workspace
    workspace="/path/to/project",        # staged into the guest as cwd
    env={"GREETING": "hi"},              # overlaid on the guest environment
)

print(result.output)     # hi, sandbox
print(result.ok)         # True  → exit_code == 0
print(result.exit_code)  # 0
```

## 3. Collect outputs

Declare `output_paths` globs; matching files in the workspace are returned in `RunResult.artifacts` after the program exits. If the program writes a `result.json`, it is parsed into `RunResult.result`.

```python
result = Sandbox(backend="local").run(
    ["python", "run_task.py", "--mode", "fast"],
    workspace="/path/to/project",
    output_paths=["out/*.json"],         # collected into result.artifacts
)

for name, data in result.artifacts.items():
    print(name, len(data))
```

## Runnable drivers

The repository ships complete, runnable drivers under `examples/`:

- `examples/run_hello_in_sandbox.py` — the bare mechanics above, on the base image.
- `examples/run_agent_in_sandbox.py` — run a provider's agent isolated in a Kata microVM.

The `local` backend needs a sandbox image in its dedicated containerd; provision it with `scripts/local-backend-up.sh` (which writes `local.env`), then `set -a; source local.env; set +a` before running a driver. See **[Getting Started](https://deepbluecoding.github.io/resoluto-sandbox/main/getting-started/index.md)** for the backend details.
