"""Unit tests for K8sSandboxRuntime._network_policy — pure function, no cluster."""
import pytest

from resoluto_sandbox.contracts import SandboxLaunchSpec
from resoluto_sandbox.runtime.k8s import EgressConfig, K8sSandboxRuntime


_STORE_CIDR = "10.0.0.1/32"
_LLM_CIDR = "10.0.0.2/32"
_GIT_CIDR = "10.0.0.3/32"
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


def _policy(*, git_cidrs: list[str] | None = None) -> dict:
    effective_git = [_GIT_CIDR] if git_cidrs is None else git_cidrs
    egress = EgressConfig(
        store_cidr=_STORE_CIDR,
        llm_cidr=_LLM_CIDR,
        git_cidrs=effective_git,
    )
    rt = _runtime(egress)
    spec = _spec({"app": "lane"})
    return rt._network_policy(spec, "pod-x", "uid-x")


def test_policy_type_is_egress_only():
    policy = _policy()
    assert policy["spec"]["policyTypes"] == ["Egress"]


def test_pod_selector_matches_spec_labels():
    policy = _policy()
    assert policy["spec"]["podSelector"]["matchLabels"] == {"app": "lane"}


def test_exactly_four_egress_rules_with_one_git_cidr():
    policy = _policy(git_cidrs=[_GIT_CIDR])
    assert len(policy["spec"]["egress"]) == 4


def test_store_llm_git_rules_are_tcp_443():
    policy = _policy()
    rules = policy["spec"]["egress"]
    for rule in rules[:3]:
        assert rule["ports"] == [{"port": 443, "protocol": "TCP"}]


def test_dns_rule_is_udp_53():
    policy = _policy()
    dns_rule = policy["spec"]["egress"][3]
    assert dns_rule["ports"] == [{"port": 53, "protocol": "UDP"}]


def test_every_rule_except_contains_imds():
    policy = _policy()
    for rule in policy["spec"]["egress"]:
        assert rule["to"][0]["ipBlock"]["except"] == [_IMDS]


def test_store_cidr_present_in_rules():
    policy = _policy()
    cidrs = [rule["to"][0]["ipBlock"]["cidr"] for rule in policy["spec"]["egress"]]
    assert _STORE_CIDR in cidrs


def test_llm_cidr_present_in_rules():
    policy = _policy()
    cidrs = [rule["to"][0]["ipBlock"]["cidr"] for rule in policy["spec"]["egress"]]
    assert _LLM_CIDR in cidrs


def test_git_cidr_present_in_rules():
    policy = _policy()
    cidrs = [rule["to"][0]["ipBlock"]["cidr"] for rule in policy["spec"]["egress"]]
    assert _GIT_CIDR in cidrs


def test_two_git_cidrs_produce_five_rules():
    policy = _policy(git_cidrs=["10.0.0.3/32", "10.0.0.4/32"])
    assert len(policy["spec"]["egress"]) == 5


def test_no_git_cidrs_produce_three_rules():
    policy = _policy(git_cidrs=[])
    assert len(policy["spec"]["egress"]) == 3


def test_egress_config_rejects_non_cidr_store():
    with pytest.raises(ValueError, match="not a CIDR"):
        EgressConfig(store_cidr="nocidr", llm_cidr="10.0.0.2/32")


def test_egress_config_rejects_non_cidr_llm():
    with pytest.raises(ValueError, match="not a CIDR"):
        EgressConfig(store_cidr="10.0.0.1/32", llm_cidr="api.anthropic.com")


def test_egress_config_rejects_non_cidr_git():
    with pytest.raises(ValueError, match="not a CIDR"):
        EgressConfig(
            store_cidr="10.0.0.1/32",
            llm_cidr="10.0.0.2/32",
            git_cidrs=["github.com"],
        )


def test_network_policy_kind_and_api_version():
    policy = _policy()
    assert policy["kind"] == "NetworkPolicy"
    assert policy["apiVersion"] == "networking.k8s.io/v1"


def test_network_policy_namespace_matches_runtime():
    rt = _runtime(EgressConfig(store_cidr=_STORE_CIDR, llm_cidr=_LLM_CIDR))
    spec = _spec({"app": "lane"})
    policy = rt._network_policy(spec, "pod-x", "uid-x")
    assert policy["metadata"]["namespace"] == "resoluto-sandboxes"
