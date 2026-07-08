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
