# Security Policy

`resoluto-sandbox` isolates untrusted programs; a sandbox-escape or egress-bypass is treated as a
top-severity issue.

## Supported versions

Pre-1.0: only the latest published release receives security fixes.

| Version  | Supported |
|----------|-----------|
| latest   | ✅         |
| < latest | ❌         |

## Reporting a vulnerability

Please report privately — do **not** open a public issue.

- Preferred: GitHub **Private Vulnerability Reporting** — repo → **Security** → **Report a vulnerability**.
- Or email **juanma@deepbluecoding.com** with a description and a reproduction.

We acknowledge within **72 hours**, keep you updated, and aim to ship a fix and coordinated
disclosure within **90 days** of a validated report. Releases are published via PyPI Trusted
Publishing (OIDC) with PEP 740 attestations — no long-lived API tokens.
