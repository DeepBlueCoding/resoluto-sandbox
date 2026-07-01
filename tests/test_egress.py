"""The backend-neutral egress allowlist + its per-provider renderers (k8s + local)."""
import pytest

from resoluto_sandbox.egress import (
    EgressConfig,
    k8s_egress_rules,
    local_egress_iptables,
    resolve_cidrs,
)


# ── k8s renderer ─────────────────────────────────────────────────────────────


def test_k8s_default_denies_public_https():
    # SECURE BY DEFAULT: EgressConfig() => store + DNS only, no blanket :443.
    rules = k8s_egress_rules(EgressConfig(store_cidr="10.0.0.5/32", store_port=9100))
    assert rules[0]["to"] == [{"ipBlock": {"cidr": "10.0.0.5/32"}}]
    assert rules[0]["ports"] == [{"port": 9100, "protocol": "TCP"}]
    assert not any(r["ports"] == [{"port": 443, "protocol": "TCP"}] for r in rules)  # NO public 443 by default
    assert any({"port": 53, "protocol": "UDP"} in r["ports"] for r in rules)          # DNS always


def test_k8s_public_https_true_opens_443():
    rules = k8s_egress_rules(EgressConfig(store_cidr="10.0.0.5/32", store_port=9100, public_https=True))
    r443 = next(r for r in rules if r["ports"] == [{"port": 443, "protocol": "TCP"}])
    assert r443["to"][0]["ipBlock"] == {"cidr": "0.0.0.0/0", "except": ["169.254.169.254/32"]}


def test_k8s_no_store_no_public_https_is_dns_only():
    rules = k8s_egress_rules(EgressConfig())   # nothing configured => only DNS reaches out
    cidrs = [t["ipBlock"]["cidr"] for r in rules for t in r["to"]]
    assert cidrs == ["0.0.0.0/0"]              # DNS rule only
    assert all({"port": 53, "protocol": "UDP"} in r["ports"] for r in rules)


def test_k8s_allow_adds_rule():
    rules = k8s_egress_rules(EgressConfig(store_cidr="10.0.0.5/32", allow=["198.51.100.0/24"], allow_port=22))
    allow = next(r for r in rules if r["ports"] == [{"port": 22, "protocol": "TCP"}])
    assert allow["to"] == [{"ipBlock": {"cidr": "198.51.100.0/24"}}]


# ── local (iptables) renderer ────────────────────────────────────────────────


def _joined(rules):
    return [" ".join(r) for r in rules]


def test_local_default_denies_public_https():
    # SECURE BY DEFAULT: no :443 ACCEPT unless opted in; DNS + IMDS/RFC1918 denies + default-deny stand.
    rules = _joined(local_egress_iptables(EgressConfig(), chain="EG"))
    assert rules[0].endswith("ESTABLISHED,RELATED -j ACCEPT")
    assert "-A EG -p udp --dport 53 -j ACCEPT" in rules
    assert "-A EG -d 169.254.0.0/16 -j REJECT" in rules          # IMDS
    assert "-A EG -d 10.0.0.0/8 -j REJECT" in rules               # RFC1918
    assert "-A EG -p tcp --dport 443 -j ACCEPT" not in rules      # deny by default
    assert rules[-1] == "-A EG -j REJECT"                         # default-deny last


def test_local_public_https_true_opens_443():
    rules = _joined(local_egress_iptables(EgressConfig(public_https=True), chain="EG"))
    assert "-A EG -p tcp --dport 443 -j ACCEPT" in rules


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


# ── provider presets (anthropic/openai/npm/pypi/…) ───────────────────────────


def test_expand_presets_names_hosts_and_passthrough():
    from resoluto_sandbox.egress import expand_presets
    out = expand_presets(["anthropic", "npm", "pypi", "example.com", "10.0.0.0/8"])
    assert "api.anthropic.com" in out
    assert "registry.npmjs.org" in out
    assert "pypi.org" in out and "files.pythonhosted.org" in out   # multi-host preset
    assert "example.com" in out and "10.0.0.0/8" in out            # non-presets pass through


def test_bundle_presets_llms_and_registries():
    from resoluto_sandbox.egress import expand_presets
    llms = expand_presets(["llms"])
    assert "api.anthropic.com" in llms and "api.openai.com" in llms and "openrouter.ai" in llms
    assert "registry.npmjs.org" in expand_presets(["registries"])


def test_allow_preset_resolves_into_a_rule(monkeypatch):
    import socket
    monkeypatch.setattr(socket, "getaddrinfo", lambda h, *a, **k: [(2, 1, 6, "", ("104.18.0.7", 0))])
    # a locked-down lane that may reach only the Anthropic API
    rules = local_egress_iptables(EgressConfig(allow=["anthropic"], public_https=False), chain="EG")
    joined = [" ".join(r) for r in rules]
    assert "-A EG -p tcp --dport 443 -d 104.18.0.7/32 -j ACCEPT" in joined
    assert "-A EG -p tcp --dport 443 -j ACCEPT" not in joined  # public_https off
