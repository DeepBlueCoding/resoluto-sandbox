# Concepts

## The program contract

A sandbox program reads `argv`, writes to `stdout` / files, and exits with a code. It never imports `resoluto.sandbox`. A script that works as `uv run agent.py` on your machine works unchanged inside the sandbox; test runners, LLM agents, and shell scripts all qualify.

Dependencies are your program's concern — put `uv run`/`pip install` in your argv, or use a prebuilt image.

______________________________________________________________________

## Run lifecycle

```text
stage → run → collect
```

1. **Stage** — the `Sandbox` resolves which backend to use, then stages the workspace into the `Conduit` (a bind-mounted directory for local, an S3 prefix for k8s).
1. **Run** — the program executes. On `backend="local"` this is a Kata microVM launched via `nerdctl` against a standalone containerd on this host (single host, no cluster) sharing a bind-mounted `LocalConduit`. On `backend="k8s"` this is a Kata microVM pod. In both cases the in-sandbox `runner_main` stages inputs, runs the program, and ships output via the `Conduit`. Live output streams to `stream` (default `sys.stdout`). `stdin` is unsupported on either backend.
1. **Collect** — when the program exits, `RunResult` is assembled from the captured output, exit code, any `output_paths` globs matched in the workspace, and a parsed `result.json` if the program wrote one.

______________________________________________________________________

## Backends

`Sandbox` holds one backend (selected by name or injected). One `SubstrateBackend` drives both backends; the only thing that varies is the injected `SandboxRuntime`.

### `local`

`backend="local"` builds a `SubstrateBackend` wired to a `KataNerdctlSandboxRuntime` and a `LocalConduit`. The program runs in a Kata microVM launched via `nerdctl` against a dedicated, standalone containerd on this host; the host and microVM share a bind-mounted directory as the conduit. Single host, no cluster; egress is enforced host-side on its CNI bridge (default-deny).

The image must contain python + the resoluto-sandbox wheel + your program's deps. Default: `resoluto-sandbox-base:<installed wheel version>` (`client.default_local_image()`) — computed from the running package version, never a hardcoded `:dev`/`:latest` tag; held in this host's containerd, never pulled from a registry. Pass `image=` to override.

### `k8s`

Each run executes in a short-lived Kata microVM pod via the `drive_node` primitive and a `Conduit` object store. The sandbox reports progress as append-only JSONL chunks to the store; the host tails and reaps the pod when done. No long-lived connection between the halves.

Inject a configured `SubstrateBackend` — the image is not a `Sandbox` constructor concern. See [Getting started → The k8s backend](https://deepbluecoding.github.io/resoluto-sandbox/main/getting-started/#the-k8s-backend) for the full wiring.

Requirements: a Kubernetes cluster (k3s, kind, EKS, or any distribution) with Kata Containers, `RESOLUTO_STORE_KIND` (plus the matching store env vars) set in the environment, and `RESOLUTO_SANDBOX_KUBECONTEXT` pinned (fails closed otherwise). `stdin` raises `NotImplementedError`, so dependencies must be baked into the image. `RunResult.errors` is always empty; the in-pod runner merges stdout and stderr into the output stream.

The language-neutral wire format is documented in `spec/PROTOCOL.md`.

______________________________________________________________________

## Conduit

A `Conduit` is the exchange medium between the host and the sandbox. It is a durable key/value store with three operations: `put`, `get`, and `list_prefix`. Implementations:

| Class           | Use                                                                                   |
| --------------- | ------------------------------------------------------------------------------------- |
| `StdoutConduit` | write-only; emits chunks to stdout — useful for piping                                |
| `LocalConduit`  | local filesystem; zero infra, for dev and tests (local backend bind-mount)            |
| `S3Conduit`     | S3 / minio-compatible — the proven k8s backend rendezvous                             |
| `GcsConduit`    | Google Cloud Storage — **provided, unverified** (experimental; not tested end-to-end) |

Chunks are immutable and append-only. The reader tails via `list_prefix` + whole-object `get`; no streaming or long-lived TCP connection is required. Any blob store with list + read-after-write can be a backend; a new one is a single subclass.

The wire encoding is UTF-8 JSON for structured objects and gzip-tar for file archives. See `spec/PROTOCOL.md` for the full key namespace and JSON Schemas.
