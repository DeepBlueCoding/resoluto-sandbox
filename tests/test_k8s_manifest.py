"""The pod manifest must carry activeDeadlineSeconds ONLY when the spec sets one —
no hidden wall-clock deadline on lanes (liveness is the watchdog, not a timer)."""
import pytest

from resoluto_sandbox.contracts import SandboxLaunchSpec
from resoluto_sandbox.runtime.k8s import EgressConfig, K8sSandboxRuntime


def test_launch_spec_default_has_no_deadline():
    spec = SandboxLaunchSpec(image="img:dev", store_prefix="run/r/nodes/n")
    assert spec.deadline_seconds is None


def test_manifest_omits_active_deadline_when_none():
    rt = K8sSandboxRuntime()
    spec = SandboxLaunchSpec(image="img:dev", store_prefix="run/r/nodes/n")
    manifest = rt._manifest(spec, "sbx-test")
    assert "activeDeadlineSeconds" not in manifest["spec"]

    capped = SandboxLaunchSpec(image="img:dev", store_prefix="run/r/nodes/n", deadline_seconds=900)
    manifest_capped = rt._manifest(capped, "sbx-test")
    assert manifest_capped["spec"]["activeDeadlineSeconds"] == 900


# ── NetworkPolicy tests ──────────────────────────────────────────────────────


def test_network_policy_default_deny_egress():
    rt = K8sSandboxRuntime(egress=EgressConfig(store_cidr="10.0.0.1/32", llm_cidr="10.0.0.2/32"))
    spec = SandboxLaunchSpec(image="img:dev", store_prefix="run/r/nodes/n", labels={"app": "lane"})
    policy = rt._network_policy(spec, "sbx-test", "fake-uid-123")
    assert policy["spec"]["policyTypes"] == ["Egress"]
    assert policy["kind"] == "NetworkPolicy"
    assert policy["apiVersion"] == "networking.k8s.io/v1"


def test_network_policy_exact_peers_store_llm_git_dns():
    rt = K8sSandboxRuntime(egress=EgressConfig(
        store_cidr="10.0.0.1/32",
        llm_cidr="10.0.0.2/32",
        git_cidrs=["10.0.0.3/32"],
    ))
    spec = SandboxLaunchSpec(image="img:dev", store_prefix="run/r/nodes/n")
    policy = rt._network_policy(spec, "sbx-test", "fake-uid")
    rules = policy["spec"]["egress"]
    assert len(rules) == 4
    assert rules[0]["to"][0]["ipBlock"]["cidr"] == "10.0.0.1/32"
    assert rules[0]["ports"][0]["port"] == 443
    assert rules[0]["ports"][0]["protocol"] == "TCP"
    assert rules[1]["to"][0]["ipBlock"]["cidr"] == "10.0.0.2/32"
    assert rules[1]["ports"][0]["port"] == 443
    assert rules[2]["to"][0]["ipBlock"]["cidr"] == "10.0.0.3/32"
    assert rules[2]["ports"][0]["port"] == 443
    assert rules[3]["ports"][0]["port"] == 53
    assert rules[3]["ports"][0]["protocol"] == "UDP"


def test_network_policy_imds_blocked_in_all_rules():
    rt = K8sSandboxRuntime(egress=EgressConfig(
        store_cidr="10.0.0.1/32",
        llm_cidr="10.0.0.2/32",
        git_cidrs=["10.0.0.3/32"],
    ))
    spec = SandboxLaunchSpec(image="img:dev", store_prefix="run/r/nodes/n")
    policy = rt._network_policy(spec, "sbx-test", "fake-uid")
    for rule in policy["spec"]["egress"]:
        for peer in rule["to"]:
            assert peer["ipBlock"]["except"] == ["169.254.169.254/32"]


def test_network_policy_zero_git_hosts():
    rt = K8sSandboxRuntime(egress=EgressConfig(store_cidr="10.0.0.1/32", llm_cidr="10.0.0.2/32"))
    spec = SandboxLaunchSpec(image="img:dev", store_prefix="run/r/nodes/n")
    policy = rt._network_policy(spec, "sbx-test", "fake-uid")
    rules = policy["spec"]["egress"]
    assert len(rules) == 3
    for rule in rules:
        assert rule is not None
        assert rule["to"]


def test_network_policy_config_driven():
    rt1 = K8sSandboxRuntime(egress=EgressConfig(
        store_cidr="192.168.1.100/32", llm_cidr="10.0.0.2/32"
    ))
    spec = SandboxLaunchSpec(image="img:dev", store_prefix="run/r/nodes/n")
    p1 = rt1._network_policy(spec, "sbx", "uid-1")
    assert p1["spec"]["egress"][0]["to"][0]["ipBlock"]["cidr"] == "192.168.1.100/32"

    rt2 = K8sSandboxRuntime(egress=EgressConfig(
        store_cidr="10.0.0.1/32",
        llm_cidr="10.0.0.2/32",
        git_cidrs=["10.0.0.3/32", "10.0.0.4/32"],
    ))
    p2 = rt2._network_policy(spec, "sbx", "uid-2")
    assert len(p2["spec"]["egress"]) == 5


def test_network_policy_owner_reference():
    rt = K8sSandboxRuntime(egress=EgressConfig(store_cidr="10.0.0.1/32", llm_cidr="10.0.0.2/32"))
    spec = SandboxLaunchSpec(image="img:dev", store_prefix="run/r/nodes/n")
    policy = rt._network_policy(spec, "my-pod", "my-pod-uid-456")
    refs = policy["metadata"]["ownerReferences"]
    assert len(refs) == 1
    assert refs[0]["kind"] == "Pod"
    assert refs[0]["name"] == "my-pod"
    assert refs[0]["uid"] == "my-pod-uid-456"
    assert refs[0]["blockOwnerDeletion"] is True


def test_egress_config_requires_cidrs():
    with pytest.raises(ValueError, match="CIDR"):
        EgressConfig(store_cidr="api.anthropic.com", llm_cidr="10.0.0.2/32")

    with pytest.raises(ValueError, match="CIDR"):
        EgressConfig(store_cidr="10.0.0.1/32", llm_cidr="10.0.0.2/32", git_cidrs=["github.com"])
