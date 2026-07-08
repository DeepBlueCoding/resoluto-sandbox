# Getting started

`Sandbox` is the one class you construct. You give it a backend name (`local` or `k8s`), or inject a configured `Backend`, and call `run(argv, ...)`. The same call is identical for every backend.

## Run a program

```python
from resoluto.sandbox import Sandbox

result = Sandbox(backend="local").run(
    ["python", "-c", "print('hello from the sandbox')"]
)
print(result.output)   # hello from the sandbox
print(result.ok)       # True → exit_code == 0
print(result.exit_code)
```

The [`RunResult`](https://deepbluecoding.github.io/resoluto-sandbox/main/api/sandbox/#resoluto.sandbox.RunResult) captures the merged stdout/stderr (`output`), the `exit_code`, any files you asked to collect (`artifacts`), and a parsed `result.json` if the program wrote one (`result`). `stdin` is not supported — pass inputs via `argv`, `env`, or workspace files.

## Passing inputs and collecting outputs

```python
result = Sandbox(backend="local").run(
    ["python", "run_task.py", "--mode", "fast"],
    workspace="/path/to/project",         # staged into the sandbox as cwd
    env={"TASK_ID": "RES-42"},            # overlaid on the guest environment
    output_paths=["out/*.json"],          # globs collected into result.artifacts
)
```

`workspace` is staged into the conduit and becomes the program's working directory. `output_paths` globs are matched in the workspace after the program exits and returned in `RunResult.artifacts`.

## The local backend image

`backend="local"` runs in a Kata microVM launched via `nerdctl` against a **dedicated, standalone containerd** (not your regular Docker daemon). It needs a sandbox image present in that containerd. When `image=` is omitted it uses `default_local_image()` — the base substrate tagged to the currently installed `resoluto-sandbox` version (never a floating `:dev`/`:latest` tag):

```python
from resoluto.sandbox.client import default_local_image

Sandbox(backend="local", image=default_local_image())   # explicit; the default
```

Build it from `Dockerfile.base` (or `resoluto-sandbox image build`) and load it into the local containerd. Run argv with the **guest's** `python` and paths relative to `workspace`.

## The k8s backend

`backend="k8s"` runs each program in a short-lived Kata microVM pod, rendezvousing through an S3/minio `Conduit`. For anything beyond the defaults, inject a configured [`SubstrateBackend`](https://deepbluecoding.github.io/resoluto-sandbox/main/api/runtime/#resoluto.sandbox.SubstrateBackend):

```python
import os
from resoluto.sandbox import Sandbox
from resoluto.sandbox.backends.substrate import SubstrateBackend, store_env_for_pod
from resoluto.sandbox.conduit.factory import store_from_env
from resoluto.sandbox.runtime.k8s import K8sSandboxRuntime

runtime = K8sSandboxRuntime(
    namespace="resoluto-sandboxes",
    context=os.environ.get("RESOLUTO_SANDBOX_KUBECONTEXT"),
)
sb = Sandbox(backend=SubstrateBackend(
    runtime=runtime,
    conduit=store_from_env(),
    image="<registry>/resoluto-sandbox-base:0.1.0",
    store_env=store_env_for_pod(os.environ),
))
```

Requirements: a Kubernetes cluster with Kata Containers, `RESOLUTO_STORE_KIND` (plus the matching store env vars), and a pinned `RESOLUTO_SANDBOX_KUBECONTEXT` (fails closed otherwise).

See **[Concepts](https://deepbluecoding.github.io/resoluto-sandbox/main/concepts/index.md)** for the run lifecycle, **[Backends](https://deepbluecoding.github.io/resoluto-sandbox/main/backends/index.md)** for the substrate details, and **[Networking](https://deepbluecoding.github.io/resoluto-sandbox/main/networking/index.md)** for per-run egress control.
