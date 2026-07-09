# Contributing to resoluto-sandbox

Thanks for helping improve `resoluto-sandbox` — a store-mediated, Kata-isolated, cloud-agnostic
execution substrate. Contributions of all kinds are welcome: bug reports, docs, tests, and code.

By participating you agree to our [Code of Conduct](CODE_OF_CONDUCT.md). Please **do not** file
security issues in public — this package isolates untrusted programs, so a sandbox-escape or
egress-bypass is top-severity: follow the [Security Policy](SECURITY.md) instead.

## Dev setup

Requires Python 3.12+ and [`uv`](https://docs.astral.sh/uv/). Clone and sync everything (base +
extras + dev group):

```bash
uv sync --all-extras --dev
```

The package is self-contained — a bare clone builds and tests fully with no cluster. The heavy
backends (`k8s`, `s3`, `gcs`) are optional extras; `--all-extras` installs them so their code paths
type- and import-check locally.

## Running tests

```bash
uv run pytest
```

The default test selection excludes anything that needs live infrastructure — `pyproject.toml` sets
`addopts = "-m 'not integration'"`, so a plain `uv run pytest` runs the self-contained suite that CI
runs. Integration tests hit a **live k3s + Kata cluster** (and minio) and are opt-in via markers:

| Marker | Needs |
|--------|-------|
| `integration` | the live k3s + Kata cluster |
| `k8s` | k3s + Kata |
| `local` | nerdctl + a dedicated containerd |
| `s3` | minio |

To run them, provision the backend (`scripts/local-backend-up.sh` for `local`, or a Kata cluster for
`k8s`) and select the marker explicitly, e.g. `uv run pytest -m local` or `uv run pytest -m integration`.

## Docs

The docs site is Material for MkDocs with API autodoc and an `llms.txt` for AI agents:

```bash
uv sync --group docs
uv run mkdocs build --strict
```

`--strict` fails the build on broken links or nav gaps — the same check CI enforces before deploying.

## Code style

- **Fail fast** — no fallbacks, no silent degradation. This is a security package: isolation must
  never downgrade quietly. If something can't be proven safe, refuse rather than run unprotected.
- **No fallback code paths** and no try/except that swallows the real error.
- **Minimal comments** — only short function docstrings describing inputs/outputs; let the code speak.
- **Pydantic models end-to-end** — no manual dict construction on the wire; match the existing
  contracts.
- Match the surrounding conventions of the file you're editing. Ruff adoption is planned; until then,
  keep diffs consistent with the existing style.

## Commits & PRs

- Follow [Conventional Commits](https://www.conventionalcommits.org/) — e.g. `fix: reap pod on
  canary failure`, `feat: add GcsConduit`, `docs: clarify egress default`.
- Keep PRs focused — one concern per PR. Small, reviewable diffs merge faster.
- Add an entry under `## [Unreleased]` in [CHANGELOG.md](CHANGELOG.md) for any user-visible change.
- Make sure `uv run pytest` passes; if you touched `docs/**`, `mkdocs.yml`, or `src/**`, confirm
  `uv run mkdocs build --strict` still succeeds.

## Releases

Releases are cut by maintainers only. The ecosystem publishes **bottom-up**: `resoluto-sandbox` and
`resoluto-agent` first, then `resoluto-engine`, then `resoluto-cli`. Pushing a `v<PEP 440 version>`
tag triggers the OIDC publish workflow — a final `vX.Y.Z` tag goes to PyPI, a pre-release
(`vX.Y.Z-rcN` / `vX.Y.Z.devN`) goes to TestPyPI. No long-lived API tokens are involved.
