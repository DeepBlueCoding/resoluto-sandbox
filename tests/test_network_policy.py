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


def _spec(labels: dict) -> SandboxLaunchSpec:
    return SandboxLaunchSpec(
        image="test/image:latest",
        labels=labels,
        store_prefix="run/test/nodes/run/lane-0",
    )


def _policy(*, store_port: int = _STORE_PORT) -> dict:
    egress = EgressConfig(store_cidr=_STORE_CIDR, store_port=store_port)
    rt = _runtime(egress)
    spec = _spec({"app": "lane"})
    return rt._network_policy(spec, "pod-x", "uid-x")


def test_policy_type_is_egress_only():
    policy = _policy()
    assert policy["spec"]["policyTypes"] == ["Egress"]


def test_pod_selector_matches_spec_labels():
    policy = _policy()
    assert policy["spec"]["podSelector"]["matchLabels"] == {"app": "lane"}


def test_exactly_three_egress_rules():
    policy = _policy()
    assert len(policy["spec"]["egress"]) == 3


def test_store_rule_carries_store_port_and_store_cidr():
    rule = _policy()["spec"]["egress"][0]
    assert rule["ports"] == [{"port": _STORE_PORT, "protocol": "TCP"}]
    assert rule["to"][0]["ipBlock"]["cidr"] == _STORE_CIDR


def test_non_443_store_port_appears_in_store_rule():
    rule = _policy(store_port=9100)["spec"]["egress"][0]
    assert rule["ports"] == [{"port": 9100, "protocol": "TCP"}]


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


def test_store_cidr_present_in_rules():
    policy = _policy()
    cidrs = [rule["to"][0]["ipBlock"]["cidr"] for rule in policy["spec"]["egress"]]
    assert _STORE_CIDR in cidrs


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
