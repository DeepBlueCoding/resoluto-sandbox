# Wire Protocol

The host and the sandbox never share memory, a socket, or a process — they rendezvous only through the durable [`Conduit`](https://deepbluecoding.github.io/resoluto-sandbox/0.1.0-rc3/api/conduit/index.md). The contract below is **language-neutral**: any runtime that can read and write JSON and gzip-tar archives to the conduit can act as a host or a guest. The Python package in this repo is one reference implementation of it.

The full specification, kept in `spec/PROTOCOL.md` in the repository and embedded verbatim here:

# Host ↔ Sandbox Wire Protocol

This document describes the protocol between a host and a sandbox (guest) in language-neutral terms. A Python reference implementation lives in `src/resoluto/sandbox/`. Any language that can read/write JSON and gzip-tar archives can implement a client.

## Transport: Conduit

Communication is mediated by a **Conduit** — a durable key/value store. The interface is three operations:

- `put(key: str, data: bytes) -> void` — write an immutable object
- `get(key: str) -> bytes` — read an object by key
- `list_prefix(prefix: str) -> list[key]` — enumerate all keys under a prefix

Backends can be local filesystem, S3/MinIO, GCS, or any compatible object store. No streaming, no in-sandbox server, no long-lived TCP connection. Chunks are immutable; ordering is established by sequence numbers embedded in the key name.

Encoding is always **UTF-8 JSON** for structured objects and **gzip-tar** for file archives. No Python-specific serialization (no pickle, no msgpack) is used anywhere on the wire.

## Key Namespace

All objects for a run live under a single prefix:

```text
run/<run_id>/nodes/<node_id>/
```

| Key (relative to run prefix) | Direction      | Description                                                   |
| ---------------------------- | -------------- | ------------------------------------------------------------- |
| `inbox/<name>.tar.gz`        | host → sandbox | Workspace content, gzip-tarred                                |
| `task.json`                  | host → sandbox | Reserved; not read by the reference runner (see schema below) |
| `events-<NNNNNN>.jsonl`      | sandbox → host | Progress events, one JSON object per line                     |
| `result.json`                | sandbox → host | Final verdict and output metadata                             |
| `outbox/<name>.tar.gz`       | sandbox → host | Output artifacts, gzip-tarred                                 |
| `_manifest.json`             | sandbox → host | EOF marker: `{"total_chunks": N}`                             |

Sequence numbers in event chunk names are zero-padded six-digit integers (`000001`, `000002`, …). Chunks are immutable once written. The host tails `list_prefix` for new chunk keys, fetches each in order, and reads lines as they arrive. `_manifest.json` signals that all chunks have been written; the host stops tailing once the chunk count matches `total_chunks`.

## Schemas

| File                   | Validates                     |
| ---------------------- | ----------------------------- |
| `event.schema.json`    | Each line in `events-*.jsonl` |
| `result.schema.json`   | `result.json`                 |
| `task.schema.json`     | `task.json`                   |
| `manifest.schema.json` | `_manifest.json`              |

All schemas use JSON Schema draft 2020-12.

### SpanEvent (`events-*.jsonl`)

Each line is a UTF-8 JSON object. Required fields:

| Field            | Type   | Values                                                                                                  |
| ---------------- | ------ | ------------------------------------------------------------------------------------------------------- |
| `run_id`         | string | Identifies the run                                                                                      |
| `span_id`        | string | Unique identifier for this span                                                                         |
| `parent_span_id` | string | Parent span; empty string for root spans                                                                |
| `kind`           | string | advisory; any string permitted. Common: `run` / `phase` / `node` / `attempt` / `agent` / `tool` / `log` |
| `name`           | string | Human-readable span name (empty for log events)                                                         |
| `event`          | string | `open` / `close` / `log`                                                                                |
| `ts`             | number | Unix epoch seconds (float), stamped by the emitter                                                      |
| `status`         | string | On `close`: `success` / `failure` / other; empty otherwise                                              |
| `data`           | object | Inputs, outputs, or log payload — all sensitive fields redacted by the sandbox                          |

### NodeResult (`result.json`)

Written once by the sandbox when work is complete. The host reads this after the manifest arrives. Fields filled by the host (`observed_phase`, `reason`, `substrate_logs`) are appended from out-of-guest signals and must not be trusted as the in-guest verdict.

| Field            | Type           | Description                                      |
| ---------------- | -------------- | ------------------------------------------------ |
| `node_id`        | string         | Node identifier (empty if not applicable)        |
| `status`         | string         | `success` or `failure`                           |
| `exit_code`      | integer / null | Exit code of the main process, if available      |
| `output_archive` | string / null  | Key of the primary output archive in the conduit |
| `observed_phase` | string         | Host-observed substrate phase                    |
| `reason`         | string         | Human-readable failure reason                    |
| `substrate_logs` | string         | Forensic substrate logs (untrusted)              |

### task.json (reserved)

**Reserved — not consumed by the reference runner.** The reference runner is configured via environment variables (`RESOLUTO_WORKLOAD_ARGV`, `RESOLUTO_WORKSPACE_DIR`, `RESOLUTO_OUTPUT_PATHS`), not by reading `task.json`. The schema is published for backends that prefer a file-based contract.

| Field           | Type            | Required | Description                        |
| --------------- | --------------- | -------- | ---------------------------------- |
| `workspace_dir` | string          | yes      | Path inside the sandbox to work in |
| `prompt`        | string          | no       | Natural-language task description  |
| `env`           | object          | no       | Extra environment variables        |
| `output_paths`  | array of string | no       | Paths to collect into the outbox   |

### \_manifest.json

Written last by the sandbox to signal that all event chunks are complete.

| Field          | Type    | Required | Description                              |
| -------------- | ------- | -------- | ---------------------------------------- |
| `total_chunks` | integer | yes      | Total number of `events-*.jsonl` objects |

## Liveness

Liveness is determined by **chunk arrival rate** and **heartbeat events**, not wall-clock time.

- The sandbox emits periodic `kind=log, event=log` heartbeat records into the event stream.
- The host treats absence of new chunks for a configurable silence window as a liveness failure.
- There are no hardcoded wall-clock timeouts. If the sandbox is doing work, it keeps running.
- Unstartable sandboxes (e.g., image pull failures) are detected via substrate status polling and fail fast; sandboxes held for resource admission keep waiting until a slot is available.

## Sequence

1. Host writes `inbox/<name>.tar.gz` (workspace) and optionally `task.json` (for backends that read it).
1. Host launches the sandbox, passing the run prefix, a write-scoped token, and workload configuration via env vars.
1. Sandbox reads `inbox/`, performs its work (the reference runner is driven by env vars, not `task.json`).
1. Sandbox appends `events-<NNNNNN>.jsonl` chunks as work progresses.
1. Sandbox writes `result.json` and any `outbox/<name>.tar.gz` artifacts.
1. Sandbox writes `_manifest.json` with the final chunk count.
1. Host reads events in sequence, fetches `result.json`, collects outbox archives.
