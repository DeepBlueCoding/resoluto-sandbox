# Host ↔ Sandbox Wire Protocol

This document describes the protocol between an orchestrator (host) and a sandbox (guest) in
language-neutral terms. A Python reference implementation lives in `src/resoluto_sandbox/`.
Any language that can read/write JSON and gzip-tar archives can implement a client.

## Transport: Conduit

Communication is mediated by a **Conduit** — a durable key/value store. The interface is three
operations:

- `put(key: str, data: bytes) -> void` — write an immutable object
- `get(key: str) -> bytes` — read an object by key
- `list_prefix(prefix: str) -> list[key]` — enumerate all keys under a prefix

Backends can be local filesystem, S3/MinIO, GCS, or any compatible object store. No streaming,
no in-sandbox server, no long-lived TCP connection. Chunks are immutable; ordering is
established by sequence numbers embedded in the key name.

Encoding is always **UTF-8 JSON** for structured objects and **gzip-tar** for file archives. No
Python-specific serialization (no pickle, no msgpack) is used anywhere on the wire.

## Key Namespace

All objects for a run live under a single prefix:

```
run/<run_id>/nodes/<node_id>/
```

| Key (relative to run prefix)       | Direction       | Description                                      |
|------------------------------------|-----------------|--------------------------------------------------|
| `inbox/<name>.tar.gz`              | host → sandbox  | Workspace content, gzip-tarred                   |
| `task.json`                        | host → sandbox  | Optional task instructions (see schema below)    |
| `events-<NNNNNN>.jsonl`            | sandbox → host  | Progress events, one JSON object per line        |
| `result.json`                      | sandbox → host  | Final verdict and output metadata                |
| `outbox/<name>.tar.gz`             | sandbox → host  | Output artifacts, gzip-tarred                    |
| `_manifest.json`                   | sandbox → host  | EOF marker: `{"total_chunks": N}`                |

Sequence numbers in event chunk names are zero-padded six-digit integers (`000001`, `000002`, …).
Chunks are immutable once written. The orchestrator tails `list_prefix` for new chunk keys,
fetches each in order, and reads lines as they arrive. `_manifest.json` signals that all chunks
have been written; the orchestrator stops tailing once the chunk count matches `total_chunks`.

## Schemas

| File                  | Validates            |
|-----------------------|----------------------|
| `event.schema.json`   | Each line in `events-*.jsonl`  |
| `result.schema.json`  | `result.json`        |
| `task.schema.json`    | `task.json`          |
| `manifest.schema.json`| `_manifest.json`     |

All schemas use JSON Schema draft 2020-12.

### SpanEvent (`events-*.jsonl`)

Each line is a UTF-8 JSON object. Required fields:

| Field          | Type    | Values                                                      |
|----------------|---------|-------------------------------------------------------------|
| `run_id`       | string  | Identifies the run                                          |
| `span_id`      | string  | Unique identifier for this span                             |
| `parent_span_id` | string | Parent span; empty string for root spans                  |
| `kind`         | string  | advisory; any string permitted. Common: `run` / `phase` / `node` / `lane` / `attempt` / `gate` / `agent` / `tool` / `log` |
| `name`         | string  | Human-readable span name (empty for log events)             |
| `event`        | string  | `open` / `close` / `log`                                   |
| `ts`           | number  | Unix epoch seconds (float), stamped by the emitter          |
| `status`       | string  | On `close`: `success` / `failure` / other; empty otherwise  |
| `data`         | object  | Inputs, outputs, or log payload — all sensitive fields redacted by the sandbox |

### NodeResult (`result.json`)

Written once by the sandbox when work is complete. The orchestrator reads this after the manifest
arrives. Fields filled by the orchestrator (`observed_phase`, `reason`, `substrate_logs`) are
appended from out-of-guest signals and must not be trusted as the in-guest verdict.

| Field            | Type           | Description                                         |
|------------------|----------------|-----------------------------------------------------|
| `node_id`        | string         | Node identifier (empty if not applicable)           |
| `status`         | string         | `success` or `failure`                              |
| `exit_code`      | integer / null | Exit code of the main process, if available         |
| `output_archive` | string / null  | Key of the primary output archive in the conduit    |
| `observed_phase` | string         | Orchestrator-observed substrate phase               |
| `reason`         | string         | Human-readable failure reason                       |
| `substrate_logs` | string         | Forensic substrate logs (untrusted)                 |

### task.json (optional)

The orchestrator may write `task.json` before the sandbox starts. The sandbox reads it to
obtain work instructions.

| Field           | Type            | Required | Description                         |
|-----------------|-----------------|----------|-------------------------------------|
| `workspace_dir` | string          | yes      | Path inside the sandbox to work in  |
| `prompt`        | string          | no       | Natural-language task description   |
| `env`           | object          | no       | Extra environment variables         |
| `output_paths`  | array of string | no       | Paths to collect into the outbox    |

### _manifest.json

Written last by the sandbox to signal that all event chunks are complete.

| Field          | Type    | Required | Description                              |
|----------------|---------|----------|------------------------------------------|
| `total_chunks` | integer | yes      | Total number of `events-*.jsonl` objects |

## Liveness

Liveness is determined by **chunk arrival rate** and **heartbeat events**, not wall-clock time.

- The sandbox emits periodic `kind=log, event=log` heartbeat records into the event stream.
- The orchestrator treats absence of new chunks for a configurable silence window as a liveness
  failure.
- There are no hardcoded wall-clock timeouts. If the sandbox is doing work, it keeps running.
- Unstartable sandboxes (e.g., image pull failures) are detected via substrate status polling
  and fail fast; sandboxes held for resource admission keep waiting until a slot is available.

## Sequence

1. Orchestrator writes `inbox/<name>.tar.gz` (workspace) and optionally `task.json`.
2. Orchestrator launches the sandbox, passing the run prefix and a write-scoped token.
3. Sandbox reads `inbox/` and `task.json`, performs its work.
4. Sandbox appends `events-<NNNNNN>.jsonl` chunks as work progresses.
5. Sandbox writes `result.json` and any `outbox/<name>.tar.gz` artifacts.
6. Sandbox writes `_manifest.json` with the final chunk count.
7. Orchestrator reads events in sequence, fetches `result.json`, collects outbox archives.
