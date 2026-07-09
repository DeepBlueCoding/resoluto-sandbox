# Changelog

All notable changes to `resoluto-sandbox` are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.0rc2] - 2026-07-09

Initial public pre-release of the store-mediated, Kata-isolated execution sandbox.

### Added

- **`Sandbox` facade** — `Sandbox(backend=...).run(argv, ...)` runs an arbitrary program in isolation
  and returns a `RunResult` (`exit_code`, `output`, `artifacts`, `result`, `ok`). Programs stay plain:
  they read `argv`/env, write `stdout`/files, and never import `resoluto.sandbox`.
- **`local` backend** — Kata microVMs via `nerdctl` against a dedicated, standalone containerd
  (`KataNerdctlSandboxRuntime`), independent of Docker and k3s. Provisioned and verified by
  `scripts/local-backend-up.sh` (ends on a green Kata-microVM canary).
- **`k8s` backend** — Kata microVM pods driven through the kube API against an S3/minio store
  (`K8sSandboxRuntime`), gated behind the `[k8s]` extra.
- **Conduit storage seam** — the durable host↔sandbox exchange: `Conduit` ABC with `LocalConduit`
  (bind-mounted dir), `S3Conduit` (minio/S3, `[s3]` extra), `StdoutConduit`, and an experimental,
  unverified `GcsConduit` (`[gcs]` extra). Host and sandbox never hold a live connection — the
  sandbox writes append-only JSONL chunks + `result.json`; the host tails and reaps.
- **Egress deny-by-default** — a fresh sandbox reaches only DNS + its own store; callers opt in per
  `run()` to exact domains via an SNI proxy (`local`) or a backend-neutral `EgressConfig` →
  `NetworkPolicy` (`k8s`). An in-guest fail-closed canary verifies isolation before the program runs;
  cloud IMDS and RFC1918 ranges are rejected even on an allowlist match.
- **CLI** — `resoluto-sandbox run`, `doctor`, and `image build`/`image push`, including `--backend`,
  `--image`, `--workspace`, and `--env-file`.
- **Secrets** — `env_file`, k8s-native `SecretKeyRef`, and a guest-resolved `SecretProvider` ref path
  (ABC only today).
- **Prebuilt provider images** — `image build --provider claude|langchain|openai|all`, each pinned to
  one SDK version and tagged by it, published to the on-box registry the local backend pulls from.
- **`spec/PROTOCOL.md`** — the language-neutral host↔sandbox wire protocol, with JSON schemas for the
  event, manifest, result, and task envelopes.
- Base install is pydantic-only; heavy dependencies are gated behind the `[s3]`, `[k8s]`, and `[gcs]`
  extras. Apache-2.0 licensed.

[Unreleased]: https://github.com/DeepBlueCoding/resoluto-sandbox/compare/v0.1.0rc2...HEAD
[0.1.0rc2]: https://github.com/DeepBlueCoding/resoluto-sandbox/releases/tag/v0.1.0rc2
