"""The backend-neutral egress allowlist + its per-provider renderers (k8s + local)."""
import pytest

from resoluto_sandbox.egress import (
    EgressConfig,
    k8s_egress_rules,
    local_egress_iptables,
    resolve_cidrs,
)


# ── k8s renderer ─────────────────────────────────────────────────────────────


def test_k8s_default_is_store_public443_dns():
    rules = k8s_egress_rules(EgressConfig(store_cidr="10.0.0.5/32", store_port=9100))
    assert rules[0]["to"] == [{"ipBlock": {"cidr": "10.0.0.5/32"}}]
    assert rules[0]["ports"] == [{"port": 9100, "protocol": "TCP"}]
    assert rules[1]["ports"] == [{"port": 443, "protocol": "TCP"}]
    assert rules[1]["to"][0]["ipBlock"] == {"cidr": "0.0.0.0/0", "except": ["169.254.169.254/32"]}
    assert {"port": 53, "protocol": "UDP"} in rules[2]["ports"]


def test_k8s_public_https_false_and_no_store():
    rules = k8s_egress_rules(EgressConfig(public_https=False))  # neutral: no store (e.g. file-mounted)
    cidrs = [t["ipBlock"]["cidr"] for r in rules for t in r["to"]]
    assert "0.0.0.0/0" in cidrs            # DNS still 0.0.0.0/0
    assert not any(r["ports"] == [{"port": 443, "protocol": "TCP"}] for r in rules)  # no blanket 443
    assert all("10.0.0" not in c for c in cidrs)  # no store rule


def test_k8s_allow_adds_rule():
    rules = k8s_egress_rules(EgressConfig(store_cidr="10.0.0.5/32", allow=["198.51.100.0/24"], allow_port=22))
    allow = next(r for r in rules if r["ports"] == [{"port": 22, "protocol": "TCP"}])
    assert allow["to"] == [{"ipBlock": {"cidr": "198.51.100.0/24"}}]


# ── local (iptables) renderer ────────────────────────────────────────────────


def _joined(rules):
    return [" ".join(r) for r in rules]


def test_local_default_chain_order():
    rules = _joined(local_egress_iptables(EgressConfig(), chain="EG"))
    assert rules[0].endswith("ESTABLISHED,RELATED -j ACCEPT")
    assert "-A EG -p udp --dport 53 -j ACCEPT" in rules
    assert "-A EG -d 169.254.0.0/16 -j REJECT" in rules          # IMDS
    assert "-A EG -d 10.0.0.0/8 -j REJECT" in rules               # RFC1918
    assert "-A EG -p tcp --dport 443 -j ACCEPT" in rules          # public HTTPS (default on)
    assert rules[-1] == "-A EG -j REJECT"                         # default-deny last


def test_local_allow_precedes_rfc1918_reject():
    rules = _joined(local_egress_iptables(
        EgressConfig(allow=["10.1.2.3/32"], allow_port=6379), chain="EG"))
    allow_i = rules.index("-A EG -p tcp --dport 6379 -d 10.1.2.3/32 -j ACCEPT")
    rfc_i = rules.index("-A EG -d 10.0.0.0/8 -j REJECT")
    assert allow_i < rfc_i  # an explicit private allow wins over the RFC1918 deny


def test_local_public_https_false_drops_443_accept():
    rules = _joined(local_egress_iptables(EgressConfig(public_https=False), chain="EG"))
    assert "-A EG -p tcp --dport 443 -j ACCEPT" not in rules
    assert rules[-1] == "-A EG -j REJECT"


def test_local_hostname_allow_resolves(monkeypatch):
    import socket
    monkeypatch.setattr(socket, "getaddrinfo", lambda h, *a, **k: [(2, 1, 6, "", ("203.0.113.7", 0))])
    rules = _joined(local_egress_iptables(EgressConfig(allow=["example.com"]), chain="EG"))
    assert "-A EG -p tcp --dport 443 -d 203.0.113.7/32 -j ACCEPT" in rules


# ── from_env knobs shared by both ────────────────────────────────────────────


def test_from_store_env_reads_simple_allow_knobs():
    cfg = EgressConfig.from_store_env({
        "RESOLUTO_STORE_ENDPOINT": "http://10.0.0.5:9100",
        "RESOLUTO_STORE_EGRESS_CIDR": "10.0.0.5/32",
        "RESOLUTO_EGRESS_ALLOW": "github.com, 198.51.100.0/24",
        "RESOLUTO_EGRESS_PUBLIC_HTTPS": "0",
    })
    assert cfg.store_cidr == "10.0.0.5/32" and cfg.store_port == 9100
    assert tuple(cfg.allow) == ("github.com", " 198.51.100.0/24")
    assert cfg.public_https is False


def test_egressconfig_rejects_non_cidr_store():
    with pytest.raises(ValueError, match="not a CIDR"):
        EgressConfig(store_cidr="api.anthropic.com")
