"""Unit tests for K8sSandboxRuntime._network_policy — pure function, no cluster.

The egress policy is the fixed 3-rule model: (1) the object store on store_port,
(2) public HTTPS TCP/443 to 0.0.0.0/0 (LLM + git), (3) DNS UDP+TCP/53 to 0.0.0.0/0.
IMDS (169.254.169.254/32) is excepted on every ipBlock."""
import pytest

from resoluto_sandbox.contracts import SandboxLaunchSpec
from resoluto_sandbox.runtime.k8s import EgressConfig, K8sSandboxRuntime


_STORE_CIDR = "10.0.0.1/32"
_STORE_PORT = 9100
_IMDS = "169.254.169.254/32"


def _runtime(egress: EgressConfig) -> K8sSandboxRuntime:
    return K8sSandboxRuntime(
        egress=egress,
        node_allocatable_memory="16Gi",
    )


def _spec(labels: dict, *, egress_allow: tuple = (), egress_public_https: bool = False) -> SandboxLaunchSpec:
    # egress_allow/egress_public_https are graph-declared and travel on the SPEC — _network_policy
    # applies them from here, not from the runtime's EgressConfig (see k8s.py:_network_policy).
    return SandboxLaunchSpec(
        image="test/image:latest",
        labels=labels,
        store_prefix="run/test/nodes/run/lane-0",
        egress_allow=list(egress_allow),
        egress_public_https=egress_public_https,
    )


def _policy(*, store_port: int = _STORE_PORT) -> dict:
    # public_https=True so these tests exercise the store + public-443 + DNS rule shape (the default
    # is now deny-by-default; that is covered in tests/test_egress.py).
    egress = EgressConfig(store_cidr=_STORE_CIDR, store_port=store_port, public_https=True)
    rt = _runtime(egress)
    spec = _spec({"app": "lane"}, egress_public_https=True)
    return rt._network_policy(spec, "pod-x", "uid-x")


def test_policy_type_is_egress_only():
    policy = _policy()
    assert policy["spec"]["policyTypes"] == ["Egress"]


def test_pod_selector_matches_spec_labels():
    policy = _policy()
    assert policy["spec"]["podSelector"]["matchLabels"] == {"app": "lane"}


def test_store_rule_carries_store_port_and_store_cidr():
    rule = _policy()["spec"]["egress"][0]
    assert rule["ports"] == [{"port": _STORE_PORT, "protocol": "TCP"}]
    assert rule["to"][0]["ipBlock"]["cidr"] == _STORE_CIDR


def test_public_https_rule_is_tcp_443_to_anywhere():
    rule = _policy()["spec"]["egress"][1]
    assert rule["ports"] == [{"port": 443, "protocol": "TCP"}]
    assert rule["to"][0]["ipBlock"]["cidr"] == "0.0.0.0/0"


def test_dns_rule_is_udp_and_tcp_53_to_anywhere():
    rule = _policy()["spec"]["egress"][2]
    assert rule["ports"] == [
        {"port": 53, "protocol": "UDP"},
        {"port": 53, "protocol": "TCP"},
    ]
    assert rule["to"][0]["ipBlock"]["cidr"] == "0.0.0.0/0"


def test_broad_rules_except_imds_store_rule_has_none():
    # IMDS is excepted on the broad 0.0.0.0/0 rules (443 + DNS). The store rule is a specific host
    # CIDR, so it carries NO except (k8s requires except ⊂ cidr — IMDS isn't a subset of a /32).
    rules = _policy()["spec"]["egress"]
    assert "except" not in rules[0]["to"][0]["ipBlock"]  # store rule
    for rule in rules[1:]:                                # public-443 + DNS
        assert rule["to"][0]["ipBlock"]["except"] == [_IMDS]


def test_egress_config_default_store_port_is_443():
    policy = _policy(store_port=443)["spec"]["egress"][0]
    assert policy["ports"] == [{"port": 443, "protocol": "TCP"}]


def test_egress_config_rejects_non_cidr_store():
    with pytest.raises(ValueError, match="not a CIDR"):
        EgressConfig(store_cidr="nocidr")


def test_network_policy_kind_and_api_version():
    policy = _policy()
    assert policy["kind"] == "NetworkPolicy"
    assert policy["apiVersion"] == "networking.k8s.io/v1"


def test_network_policy_namespace_matches_runtime():
    rt = _runtime(EgressConfig(store_cidr=_STORE_CIDR))
    spec = _spec({"app": "lane"})
    policy = rt._network_policy(spec, "pod-x", "uid-x")
    assert policy["metadata"]["namespace"] == "resoluto-sandboxes"


# ── simple allowlist knobs: allow=[...] / allow_port / public_https ──────────


def _rules(egress: EgressConfig) -> list[dict]:
    # Mirror the EgressConfig's allow/public_https onto the spec — that's what _network_policy
    # actually reads (the runtime's own EgressConfig only supplies the store base).
    spec = _spec({"app": "lane"}, egress_allow=tuple(egress.allow), egress_public_https=egress.public_https)
    return _runtime(egress)._network_policy(spec, "pod-x", "uid-x")["spec"]["egress"]


def test_allow_cidr_adds_rule_on_allow_port():
    # a CIDR in `allow` is used verbatim, on allow_port (e.g. 22 for git-over-SSH)
    rules = _rules(EgressConfig(store_cidr=_STORE_CIDR, allow=["203.0.113.0/24"], allow_port=22))
    allow_rule = next(r for r in rules if r["ports"] == [{"port": 22, "protocol": "TCP"}])
    assert allow_rule["to"] == [{"ipBlock": {"cidr": "203.0.113.0/24"}}]


def test_public_https_false_drops_the_blanket_443_rule():
    rules = _rules(EgressConfig(store_cidr=_STORE_CIDR, public_https=False))
    # no 0.0.0.0/0:443 rule remains...
    assert not [r for r in rules
                if {"port": 443, "protocol": "TCP"} in r["ports"]
                and r["to"][0]["ipBlock"]["cidr"] == "0.0.0.0/0"]
    # ...but the store rule and DNS still do
    assert any(r["to"][0]["ipBlock"]["cidr"] == _STORE_CIDR for r in rules)
    assert any({"port": 53, "protocol": "UDP"} in r["ports"] for r in rules)


def test_allow_hostnames_resolve_to_cidrs(monkeypatch):
    # a hostname in `allow` is resolved to one /32 per A record (deduped)
    import socket
    monkeypatch.setattr(socket, "getaddrinfo", lambda host, *a, **k: [
        (2, 1, 6, "", ("93.184.216.34", 0)),
        (2, 1, 6, "", ("93.184.216.35", 0)),
    ])
    rules = _rules(EgressConfig(store_cidr=_STORE_CIDR, store_port=9100,
                                allow=["example.com"], public_https=False))
    allow_rule = next(r for r in rules if r["ports"] == [{"port": 443, "protocol": "TCP"}]
                      and r["to"][0]["ipBlock"]["cidr"] != "0.0.0.0/0")
    assert [t["ipBlock"]["cidr"] for t in allow_rule["to"]] == ["93.184.216.34/32", "93.184.216.35/32"]


def test_resolve_cidrs_passes_cidrs_through_and_dedupes(monkeypatch):
    import socket
    from resoluto_sandbox.egress import resolve_cidrs
    monkeypatch.setattr(socket, "getaddrinfo", lambda host, *a, **k: [
        (2, 1, 6, "", ("1.2.3.4", 0)), (2, 1, 6, "", ("1.2.3.4", 0)),  # dup A records
    ])
    assert resolve_cidrs(["10.0.0.0/8", "host", "10.0.0.0/8"]) == ["10.0.0.0/8", "1.2.3.4/32"]


def test_allow_unresolvable_host_raises():
    import pytest
    from resoluto_sandbox.egress import resolve_cidrs
    with pytest.raises(ValueError, match="cannot resolve host"):
        resolve_cidrs(["definitely-not-a-real-host.invalid"])
