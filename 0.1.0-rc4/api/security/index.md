# Security

Per-run egress policy (`EgressConfig`) and how a run references a secret without ever seeing its value (`SecretKeyRef`).

## resoluto.sandbox.egress.EgressConfig

```python
EgressConfig(
    allow=(),
    allow_port=443,
    public_https=False,
    store_cidr=None,
    store_port=443,
)
```

Backend-neutral egress allowlist. SECURE BY DEFAULT: `EgressConfig()` DENIES all egress except DNS and the object store — a fresh sandbox cannot phone home. You opt IN to what the workload needs. Same knobs on every backend:

- public_https=False (DEFAULT) → deny all outbound except DNS + store. Set **True** to allow ALL HTTPS (:443) — the "let it reach the internet" escape hatch for trusted workloads.
- allow=[...] opens SPECIFIC destinations — hostnames (e.g. "api.anthropic.com", "registry.npmjs.org") OR CIDRs — on allow_port (443 default; e.g. 22 for git-over-SSH). Hostnames resolve to CIDRs when rendered. This is the RECOMMENDED way to run untrusted code on k8s: least privilege. (On the local backend, prefer per-run `Sandbox.run(egress=[domains])` — enforced by domain via the SNI proxy, so it never goes stale for CDN-backed hosts.)
- store_cidr/store_port: the k8s object-store endpoint (REQUIRED for the k8s backend; the local backend reaches its store over a file mount, so it ignores these). Always allowed — the sandbox must return results.

IMDS (169.254.169.254) is ALWAYS denied; the local renderer also denies RFC1918 (no lateral movement) unless you explicitly `allow` a private CIDR.

NOTE: NetworkPolicy/iptables are CIDR-based — pinning a CDN-backed host (e.g. api.anthropic.com behind Cloudflare) to its resolved IPs is fragile (they rotate). When you need reliable access to such a host from otherwise-restricted code, `public_https=True` is the pragmatic choice.

### from_store_env

```python
from_store_env(env=None)
```

Build the allowlist from RESOLUTO_STORE_ENDPOINT, honoring RESOLUTO_STORE_EGRESS_CIDR/PORT and the simple RESOLUTO_EGRESS_ALLOW / RESOLUTO_EGRESS_PUBLIC_HTTPS knobs; None if no store.

Source code in `src/resoluto/sandbox/egress.py`

```python
@classmethod
def from_store_env(cls, env: "dict[str, str] | None" = None) -> "EgressConfig | None":
    """Build the allowlist from RESOLUTO_STORE_ENDPOINT, honoring RESOLUTO_STORE_EGRESS_CIDR/PORT
    and the simple RESOLUTO_EGRESS_ALLOW / RESOLUTO_EGRESS_PUBLIC_HTTPS knobs; None if no store."""
    import os
    import socket
    from urllib.parse import urlparse

    e = env if env is not None else os.environ
    allow = tuple(x for x in (e.get("RESOLUTO_EGRESS_ALLOW") or "").split(",") if x.strip())
    public_https = e.get("RESOLUTO_EGRESS_PUBLIC_HTTPS", "0").strip().lower() not in (
        "0",
        "false",
        "no",
        "",
    )
    allow_port = int(e.get("RESOLUTO_EGRESS_ALLOW_PORT", "443"))

    raw = (e.get("RESOLUTO_STORE_ENDPOINT") or "").strip()
    if not raw:
        return None
    u = urlparse(raw if "://" in raw else f"http://{raw}")
    endpoint_port = u.port or (443 if u.scheme == "https" else 80)

    override = (e.get("RESOLUTO_STORE_EGRESS_CIDR") or "").strip()
    if override:
        port = e.get("RESOLUTO_STORE_EGRESS_PORT")
        return cls(
            store_cidr=override,
            store_port=int(port) if port else endpoint_port,
            allow=allow,
            allow_port=allow_port,
            public_https=public_https,
        )
    if not u.hostname:
        return None
    try:
        ip = socket.gethostbyname(u.hostname)
    except OSError:
        return None
    return cls(
        store_cidr=f"{ip}/32",
        store_port=endpoint_port,
        allow=allow,
        allow_port=allow_port,
        public_https=public_https,
    )
```

## resoluto.sandbox.secrets.SecretKeyRef

```python
SecretKeyRef(name, key)
```

Reference to an existing Kubernetes Secret's key. k8s-only: rendered as valueFrom.secretKeyRef by K8sSandboxRuntime; ignored by the local backend (KataNerdctlSandboxRuntime never reads SandboxLaunchSpec.k8s_secret_refs). The Secret itself must already exist — created by kubectl, External Secrets Operator, or any other means; resoluto-sandbox never creates or syncs one.
