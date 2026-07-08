# Changelog

All notable changes to `resoluto-sandbox` are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/); versions follow [SemVer](https://semver.org/).

## Unreleased

## 0.1.0 — 2026-07-08

- Store-mediated, Kata-isolated execution substrate: `Sandbox(backend=...).run(argv, ...)` over a
  passive, self-reporting sandbox that rendezvous with the host through a durable `Conduit`.
- Two backends behind one `SubstrateBackend`: `local` (Kata microVM via `nerdctl` on a dedicated
  containerd) and `k8s` (short-lived Kata microVM pod).
- `Conduit` seams — `LocalConduit`, `StdoutConduit`, `S3Conduit` (minio/S3), `GcsConduit`
  (experimental) — and the `SandboxRuntime` isolation seam.
- Per-run egress control, `SandboxPool` bounded concurrency, and the `SubstrateBackend` that drives
  the stage → run → collect lifecycle.
- `GcsConduit` hardened to `S3Conduit` parity: bounded transient-fault retry, `ConduitError`
  translation, and auth-denial (401/403) passthrough; documented as a single-tenant host-side store
  with no per-prefix credential scoping.
- `store_from_env` refuses `RESOLUTO_STORE_KIND=gcs` with a scoped `RESOLUTO_STORE_WRITE_TOKEN`
  rather than silently granting whole-service-account access.
- `K8sSandboxRuntime` refuses to build an egress `NetworkPolicy` for a pod with no labels (an empty
  podSelector would apply to every pod in the namespace), and `close()` resets its cached API clients.
- Added `SOUL.md` — the substrate's design philosophy (isolation ⊥ rendezvous; the Conduit as the
  backend-portability seam).
