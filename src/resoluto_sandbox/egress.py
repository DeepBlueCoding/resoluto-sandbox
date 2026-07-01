"""Backend-neutral egress allowlist + per-provider renderers.

`EgressConfig` is the ONE abstraction every sandbox provider shares: a default-deny allowlist —
all public HTTPS (`:443`) + DNS + your extra `allow` destinations, with IMDS always denied. Each
`SandboxRuntime` renders the SAME config to its own enforcement mechanism through a pure function
here, so the policy is written once and reused everywhere:

  - k8s   → `k8s_egress_rules()`       — NetworkPolicy `ipBlock` egress rules
  - local → `local_egress_iptables()`  — host `iptables` rules on the lane CNI bridge

To support a NEW provider (firecracker, gVisor, a cloud sandbox, …), add a renderer that maps
`EgressConfig` to that provider's mechanism — no change to callers or to the config.

This module has NO platform dependencies (pure stdlib), so any runtime can import it cheaply.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

IMDS_CIDR = "169.254.169.254/32"           # cloud metadata (k8s rule `except`)
IMDS_RANGE = "169.254.0.0/16"              # whole link-local range (local REJECT)
RFC1918 = ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")

# Friendly names for the most-used LLM inference endpoints and package registries, so a locked-down
# allowlist reads `allow=["anthropic", "npm", "pypi"]`. Each expands to its API hostname(s), resolved
# to CIDRs when rendered. NOTE: these are all CDN-backed (rotating IPs) — for RELIABLE access keep
# public_https=True (the default), which allows all of them; presets are for the public_https=False
# lock-down case, where you accept periodic re-resolve. Maintained best-effort; override with your own
# hosts/CIDRs anytime.
LLM_PRESETS: dict[str, tuple[str, ...]] = {
    "anthropic": ("api.anthropic.com",),
    "openai": ("api.openai.com",),
    "openrouter": ("openrouter.ai",),
    "gemini": ("generativelanguage.googleapis.com",),
    "groq": ("api.groq.com",),
    "mistral": ("api.mistral.ai",),
    "cohere": ("api.cohere.com",),
    "deepseek": ("api.deepseek.com",),
    "together": ("api.together.xyz",),
    "perplexity": ("api.perplexity.ai",),
    "fireworks": ("api.fireworks.ai",),
    "xai": ("api.x.ai",),
}
REGISTRY_PRESETS: dict[str, tuple[str, ...]] = {
    "npm": ("registry.npmjs.org",),
    "pypi": ("pypi.org", "files.pythonhosted.org"),
    "uv": ("pypi.org", "files.pythonhosted.org", "astral.sh"),
    "composer": ("repo.packagist.org", "packagist.org"),
    "cargo": ("crates.io", "static.crates.io", "index.crates.io"),
    "go": ("proxy.golang.org", "sum.golang.org"),
    "rubygems": ("rubygems.org", "index.rubygems.org"),
    "github": ("github.com", "api.github.com", "codeload.github.com", "objects.githubusercontent.com"),
    "huggingface": ("huggingface.co", "cdn-lfs.huggingface.co"),
}
PRESETS: dict[str, tuple[str, ...]] = {
    **LLM_PRESETS,
    **REGISTRY_PRESETS,
    "llms": tuple(sorted({h for v in LLM_PRESETS.values() for h in v})),
    "registries": tuple(sorted({h for v in REGISTRY_PRESETS.values() for h in v})),
}


def expand_presets(entries: Sequence[str]) -> list[str]:
    """Expand any preset NAME (e.g. 'anthropic', 'npm', 'llms') to its hostnames; pass others through."""
    out: list[str] = []
    for raw in entries:
        e = (raw or "").strip()
        if not e:
            continue
        out.extend(PRESETS[e]) if e in PRESETS else out.append(e)
    return out


def resolve_cidrs(entries: Sequence[str]) -> list[str]:
    """Resolve allow entries to a de-duplicated list of CIDRs.

    Each entry is a preset name (expanded to hostnames), a CIDR (used verbatim), or a hostname
    (resolved to one /32 per A record). Raises ValueError if a hostname does not resolve.
    """
    import socket

    out: list[str] = []
    for raw in expand_presets(entries):
        e = (raw or "").strip()
        if not e:
            continue
        if "/" in e:
            out.append(e)
            continue
        try:
            infos = socket.getaddrinfo(e, None, family=socket.AF_INET, type=socket.SOCK_STREAM)
        except OSError as exc:
            raise ValueError(f"EgressConfig.allow: cannot resolve host {e!r}: {exc}") from exc
        out.extend(f"{info[4][0]}/32" for info in infos)
    seen: set[str] = set()
    return [c for c in out if not (c in seen or seen.add(c))]


@dataclass(frozen=True)
class EgressConfig:
    """Backend-neutral egress allowlist (default-deny). Simple knobs, same on every backend:

    - public_https=True (default) allows ALL outbound HTTPS (:443) — so github, api.anthropic.com,
      package mirrors, etc. work with NO extra config. Set False to allow ONLY what you list.
    - allow=[...] adds extra destinations — preset NAMES (e.g. "anthropic", "openai", "openrouter",
      "npm", "pypi", "composer", "github", or the bundles "llms"/"registries"; see PRESETS), hostnames,
      OR CIDRs — on allow_port (443 default; e.g. 22 for git-over-SSH). Names/hostnames resolve to CIDRs
      when rendered.
    - store_cidr/store_port: the k8s object-store endpoint (REQUIRED for the k8s backend; the local
      backend reaches its store over a file mount, so it ignores these).

    IMDS (169.254.169.254) is ALWAYS denied; the local renderer also denies RFC1918 (no lateral
    movement) unless you explicitly `allow` a private CIDR.

    NOTE: NetworkPolicy/iptables are CIDR-based — pinning a CDN-backed host (e.g. api.anthropic.com
    behind Cloudflare) to its resolved IPs is fragile (they rotate). For such hosts keep
    public_https=True rather than listing them in `allow`.
    """

    allow: Sequence[str] = ()
    allow_port: int = 443
    public_https: bool = True
    store_cidr: str | None = None
    store_port: int = 443

    def __post_init__(self) -> None:
        if self.store_cidr is not None and "/" not in self.store_cidr:
            raise ValueError(
                f"EgressConfig: store_cidr {self.store_cidr!r} is not a CIDR (missing '/'); "
                "k8s NetworkPolicy ipBlock requires CIDR notation"
            )

    @classmethod
    def from_store_env(cls, env: "dict[str, str] | None" = None) -> "EgressConfig | None":
        """Build the allowlist from RESOLUTO_STORE_ENDPOINT, honoring RESOLUTO_STORE_EGRESS_CIDR/PORT
        and the simple RESOLUTO_EGRESS_ALLOW / RESOLUTO_EGRESS_PUBLIC_HTTPS knobs; None if no store."""
        import os
        import socket
        from urllib.parse import urlparse

        e = env if env is not None else os.environ
        allow = tuple(x for x in (e.get("RESOLUTO_EGRESS_ALLOW") or "").split(",") if x.strip())
        public_https = (e.get("RESOLUTO_EGRESS_PUBLIC_HTTPS", "1").strip().lower()
                        not in ("0", "false", "no", ""))
        allow_port = int(e.get("RESOLUTO_EGRESS_ALLOW_PORT", "443"))

        raw = (e.get("RESOLUTO_STORE_ENDPOINT") or "").strip()
        if not raw:
            return None
        u = urlparse(raw if "://" in raw else f"http://{raw}")
        endpoint_port = u.port or (443 if u.scheme == "https" else 80)

        override = (e.get("RESOLUTO_STORE_EGRESS_CIDR") or "").strip()
        if override:
            port = e.get("RESOLUTO_STORE_EGRESS_PORT")
            return cls(store_cidr=override, store_port=int(port) if port else endpoint_port,
                       allow=allow, allow_port=allow_port, public_https=public_https)
        if not u.hostname:
            return None
        try:
            ip = socket.gethostbyname(u.hostname)
        except OSError:
            return None
        return cls(store_cidr=f"{ip}/32", store_port=endpoint_port,
                   allow=allow, allow_port=allow_port, public_https=public_https)


def k8s_egress_rules(cfg: EgressConfig) -> list[dict]:
    """Render `cfg` to k8s NetworkPolicy egress rules (default-deny + these allows)."""
    rules: list[dict] = []
    if cfg.store_cidr:
        rules.append({
            "ports": [{"port": cfg.store_port, "protocol": "TCP"}],
            "to": [{"ipBlock": {"cidr": cfg.store_cidr}}],
        })
    if cfg.public_https:
        rules.append({
            "ports": [{"port": 443, "protocol": "TCP"}],
            "to": [{"ipBlock": {"cidr": "0.0.0.0/0", "except": [IMDS_CIDR]}}],
        })
    rules.append({
        "ports": [{"port": 53, "protocol": "UDP"}, {"port": 53, "protocol": "TCP"}],
        "to": [{"ipBlock": {"cidr": "0.0.0.0/0", "except": [IMDS_CIDR]}}],
    })
    cidrs = resolve_cidrs(cfg.allow)
    if cidrs:
        rules.append({
            "ports": [{"port": cfg.allow_port, "protocol": "TCP"}],
            "to": [{"ipBlock": {"cidr": c}} for c in cidrs],
        })
    return rules


def local_egress_iptables(cfg: EgressConfig, *, chain: str) -> list[list[str]]:
    """Render `cfg` to ordered `iptables` rule args (each list is the args AFTER `iptables`) for the
    local lane bridge's egress `chain`. The caller creates/flushes the chain and hooks it into
    FORWARD for the bridge subnet. The local store is a file mount, so store_cidr is not used here.

    Order (first match wins): keep established, DNS, deny IMDS, then explicit `allow` (may be private,
    so it precedes the RFC1918 denies), then deny RFC1918, then public HTTPS (if enabled), then deny.
    """
    rules: list[list[str]] = [
        ["-A", chain, "-m", "state", "--state", "ESTABLISHED,RELATED", "-j", "ACCEPT"],
        ["-A", chain, "-p", "udp", "--dport", "53", "-j", "ACCEPT"],
        ["-A", chain, "-p", "tcp", "--dport", "53", "-j", "ACCEPT"],
        ["-A", chain, "-d", IMDS_RANGE, "-j", "REJECT"],
    ]
    for c in resolve_cidrs(cfg.allow):
        rules.append(["-A", chain, "-p", "tcp", "--dport", str(cfg.allow_port), "-d", c, "-j", "ACCEPT"])
    for r in RFC1918:
        rules.append(["-A", chain, "-d", r, "-j", "REJECT"])
    if cfg.public_https:
        rules.append(["-A", chain, "-p", "tcp", "--dport", "443", "-j", "ACCEPT"])
    rules.append(["-A", chain, "-j", "REJECT"])
    return rules


def _main(argv: "list[str] | None" = None) -> int:
    """CLI: emit local iptables rule-arg lines from env config, for the local-backend provisioner.

    Usage: python -m resoluto_sandbox.egress local-iptables --chain <name>
    Reads RESOLUTO_EGRESS_ALLOW (comma list of host/CIDR), RESOLUTO_EGRESS_ALLOW_PORT,
    RESOLUTO_EGRESS_PUBLIC_HTTPS. Prints one rule per line (args after `iptables`), for the
    provisioner to apply with `sudo iptables $line`.
    """
    import argparse
    import os

    p = argparse.ArgumentParser(prog="resoluto_sandbox.egress")
    sub = p.add_subparsers(dest="cmd")
    lp = sub.add_parser("local-iptables")
    lp.add_argument("--chain", required=True)
    args = p.parse_args(argv)
    if args.cmd != "local-iptables":
        p.print_help()
        return 2

    cfg = EgressConfig(
        allow=tuple(x for x in (os.environ.get("RESOLUTO_EGRESS_ALLOW") or "").split(",") if x.strip()),
        allow_port=int(os.environ.get("RESOLUTO_EGRESS_ALLOW_PORT", "443")),
        public_https=(os.environ.get("RESOLUTO_EGRESS_PUBLIC_HTTPS", "1").strip().lower()
                      not in ("0", "false", "no", "")),
    )
    for rule in local_egress_iptables(cfg, chain=args.chain):
        print(" ".join(rule))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
