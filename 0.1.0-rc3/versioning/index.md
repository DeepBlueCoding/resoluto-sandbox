# Versioning & releases

Every `resoluto-*` package follows [Semantic Versioning](https://semver.org/) and is versioned **independently** — there is no lockstep release train.

## Pre-1.0 (`0.y.z`)

While a package is `0.y.z`:

- **`0.MINOR`** may introduce breaking changes — always called out in the [changelog](https://deepbluecoding.github.io/resoluto-sandbox/0.1.0-rc3/changelog/index.md).
- **`0.x.PATCH`** is bug-fixes only, never breaking.
- The **latest minor always upgrades cleanly** from the previous one within the same `0.x` line.

`1.0.0` is a stability promise, not a maturity milestone — it ships when the public API is one we're ready to keep.

## Deprecations

A public API slated for removal emits a `DeprecationWarning` for at least one minor release before it goes.

## Releases

Releases are **tag-driven** and published via **PyPI Trusted Publishing (OIDC)** — no stored tokens.

- Final tags (`vX.Y.Z`) publish to **PyPI**.
- Pre-release tags (`vX.Y.Z-rcN`, `vX.Y.Z.devN`) publish to **TestPyPI** for verification first.

Cross-package dependencies are pinned to compatible ranges, so installing one package pulls compatible siblings.
