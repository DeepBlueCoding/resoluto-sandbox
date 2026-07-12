# Changelog

All notable changes to `resoluto-sandbox` are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.0rc8] - 2026-07-12

### Security

- **`store_prefix` mount-escape guard** — `KataNerdctlSandboxRuntime.launch` now rejects a
  `store_prefix` containing `..` or an absolute component before building the prefix-scoped conduit
  mount. `store_prefix` is caller-set (the facade uses `run/<uuid>`), but the runtime must never
  construct `<conduit>/<prefix>` that resolves outside the conduit root and binds an arbitrary host
  directory into the guest. Defense-in-depth for the scoped mount introduced in rc6/rc7.

## [0.1.0rc7] - 2026-07-12

### Security

- **Prefix pre-create moved into the runtime** — the world-writable scoped-mount source is now created
  in `KataNerdctlSandboxRuntime.launch` itself, so the contract travels with the runtime for EVERY
  caller (the `SubstrateBackend` facade AND the engine's lane substrate). Previously only the facade
  pre-created it, so a no-workspace engine step (a gate/resume step) would have hit a root-owned mount
  the guest could not write. Removes the interim `.keep` sentinel (and its extra object on the S3 path).
- **`GcsConduit.copy_prefix` subtree scoping** — same sibling-prefix bleed fix as `S3Conduit` (`run/A`
  no longer matches `run/AB/…`).

## [0.1.0rc6] - 2026-07-12

### Security

Three findings from an internal red-team of the guest→host and cross-run boundaries:

- **Prefix-scoped conduit mount (`local`)** — the Kata guest now bind-mounts only its **own run
  prefix** (`<conduit>/<prefix>:/conduit/<prefix>`) instead of the whole conduit root. A guest can no
  longer read or write sibling runs/lanes that share a conduit root (cross-run credential/artifact
  read + prefix poisoning). The substrate pre-creates the world-writable prefix via a `.keep` sentinel
  so a `workspace=None` run still has a writable mount source. Host-side reads/resume are unaffected
  (they key on full prefixes against the conduit root).
- **Declared-output containment (host ingest)** — `fetch_outputs` now materializes **only the members
  matching the caller's declared `output_paths`** from the guest-authored outbox archive. A malicious
  guest (which has RW on its conduit prefix) can no longer smuggle undeclared files — e.g. a poisoned
  `.git/config` that yields deferred host code-exec on the operator's next `git` — into the caller's
  workspace. Extraction stays traversal-safe (`filter="data"`).
- **`copy_prefix` subtree scoping (`S3`)** — S3 lists by raw string prefix, so `run/A` also matched a
  sibling `run/AB/…`; `copy_prefix` now scopes to the real subtree (trailing slash), so a resume never
  drags a sibling run's objects along.

## [0.1.0rc5] - 2026-07-12

### Changed

- **`local` backend defaults to `--network none`** — a deny-all run (the default; no `egress=` allowlist)
  now launches the Kata microVM with no NIC at all. The store is a `virtiofs` bind, so the sandbox is
  fully functional with zero network. This means the common case needs **no host firewall, no iptables,
  no SNI-proxy provisioning, and no domains file** — it works out of the box. A non-empty `egress=`
  allowlist still uses the bridge + SNI-proxy path. `apply_egress([])` no longer writes (or requires)
  the egress-domains file.

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
