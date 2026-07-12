# Security Policy

`resoluto-sandbox` isolates untrusted programs; a sandbox-escape or egress-bypass is treated as a
top-severity issue.

## Security model

Every workload is treated as hostile and granted nothing by default:

- **Own kernel.** Each run is a Kata microVM — a hardware-virtualized guest with its own Linux kernel,
  not a shared-kernel container. A guest-kernel exploit stops at the VM.
- **Unprivileged.** The workload runs as a non-root user with no Linux capabilities and no privilege
  escalation. The docker-in-docker mode is guest-scoped (`privileged-without-host-devices`) and still
  drops the workload to a non-root user.
- **No ambient host access.** No host filesystem, devices, kernel memory, or control-plane sockets; its
  own PID namespace; no host credentials forwarded — only a scoped, short-lived store token.
- **Isolated per run.** Each run is confined to its own store prefix and cannot read another run's state.
- **Deny-by-default egress.** No network by default; specific domains are granted per run. Cloud
  metadata (IMDS) and RFC1918 ranges are refused even on an allowlist match.
- **Contained I/O.** Inputs and outputs cross the boundary as data, never control. The host never
  executes workload output; extraction is scoped to declared paths and rejects traversal/symlink escapes.
- **No silent downgrade.** The Kata runtime-class guard is unconditional and egress is fail-closed; a
  fail-closed canary proves isolation is in force before the workload runs.

## Threat model

In scope: escape from the microVM to the host, cross-run or cross-tenant data access, egress-policy
bypass, host code execution through the data channel (store, output archive, telemetry), and privilege
escalation across the boundary.

The trusted computing base is the hypervisor and host kernel (KVM / Kata) — the standard assumption of
any virtualization or serverless platform. A vulnerability in the isolation host itself is addressed by
patching, not by this package; keep the isolation host current.

Out of scope: what a workload does with access you deliberately grant it — an opened egress domain, a
credential you pass in, a path you stage. That is your policy to set, not a boundary the sandbox enforces.

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
