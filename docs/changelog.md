# Changelog

All notable changes to `resoluto-sandbox` are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/); versions follow [SemVer](https://semver.org/).

## Unreleased

## 0.2.0rc1 — 2026-10-12

- `Conduit.put_if_absent` / `Conduit.compare_and_swap`: conditional writes for durable ownership
  records — atomic on `LocalConduit`, S3 conditional requests on `S3Conduit`.
- dind on Kata: bounded guest ext4 graphs for overlay2, the visible guest graph mount inspected by
  kernel identity, and DIND pod identity preserved when the graph environment is added.
- Telemetry: the sequence advances only after durable chunk writes, events arriving while chunks
  are written are retained, and the stream drains before the closing manifest is committed.
- `stage_inputs` restores the owner-write bit on staged directories.
- Release tooling: only a bare `vX.Y.Z` tag routes to PyPI; OIDC `id-token` scoped to the publish
  jobs; explicit sdist allowlist (local caches no longer leak into the sdist).

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
