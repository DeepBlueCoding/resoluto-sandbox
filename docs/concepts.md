# Concepts

---

## The program contract

A sandbox program is **plain**. It reads `argv` / `stdin`, writes to `stdout` / files, and
exits with a code. It never imports `resoluto_sandbox`. This decoupling is the core guarantee:
a script that works as `uv run agent.py` on your machine works unchanged inside the sandbox.
Test runners, LLM agents, shell scripts — all qualify.

---

## Run lifecycle

```
stage → run → collect
```

1. **Stage** — the `Sandbox` resolves which backend to use and wraps the argv through the
   `Deps` strategy (e.g. prepending `uv run` for PEP 723 scripts).
2. **Run** — the program executes. On `backend="local"` this is a subprocess on the host
   that inherits the host environment and streams stdout live to `stream` (default
   `sys.stdout`). The program's stdin, env overlay, and working directory are supplied as
   arguments to `run()`.
3. **Collect** — when the program exits, `RunResult` is assembled from the captured stdout,
   stderr, exit code, any `output_paths` globs matched in the workspace, and a parsed
   `result.json` if the program wrote one.

---

## Backends

### `local` (available now)

The program runs as a subprocess on the calling host. The host environment is inherited
(env overlay is additive, not a replacement), so an agent CLI that is already logged in
on the host authenticates with no extra wiring. There is no isolation from the host
filesystem or network — `local` is a development and integration convenience, not a
security boundary.

### `k8s` (design / roadmap)

Each run executes in a short-lived Kata microVM pod. The sandbox reports progress as
append-only JSONL chunks to an object store; the orchestrator tails the store and reaps
the pod when done. There is no long-lived connection between the two halves. The
language-neutral wire format is already published in `spec/PROTOCOL.md`; the Python
`backend="k8s"` path raises `NotImplementedError` in the current build.

---

## Conduit

A `Conduit` is the exchange medium between the host and the sandbox. It is a durable
key/value store with three operations: `put`, `get`, and `list_prefix`. Implementations:

| Class | Use |
|---|---|
| `StdoutConduit` | write-only; emits chunks to stdout — useful for piping |
| `LocalConduit` | local filesystem; zero infra, for dev and tests |
| `S3Conduit` | S3 / MinIO — production rendezvous |
| `GcsConduit` | Google Cloud Storage |

Chunks are immutable and append-only. The reader tails via `list_prefix` + whole-object
`get`; no streaming or long-lived TCP connection is required. Any blob store with list +
read-after-write can be a backend; a new one is a single subclass.

The wire encoding is UTF-8 JSON for structured objects and gzip-tar for file archives.
See `spec/PROTOCOL.md` for the full key namespace and JSON Schemas.

---

## Dependency strategies

`Deps(kind=...)` controls how a program's dependencies are resolved at launch time:

- `auto` — detect from the script or workspace (PEP 723 inline, `requirements.txt`,
  `pyproject.toml`).
- `inline` / `requirements` — delegate to `uv run`.
- `image` / `vendored` — deps are already present; argv is passed through unchanged.
