"""The pod manifest must carry activeDeadlineSeconds ONLY when the spec sets one —
no hidden wall-clock deadline on lanes (liveness is the watchdog, not a timer)."""
import logging

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


# ── ownerReferences on pod manifest ─────────────────────────────────────────


def test_manifest_with_owner_has_configmap_owner_reference():
    rt = K8sSandboxRuntime()
    spec = SandboxLaunchSpec(image="img:dev", store_prefix="run/r/nodes/n")
    manifest = rt._manifest(spec, "sbx-test", owner_name="run-owner-abc", owner_uid="cm-uid-123")
    refs = manifest["metadata"]["ownerReferences"]
    assert len(refs) == 1
    assert refs[0]["apiVersion"] == "v1"
    assert refs[0]["kind"] == "ConfigMap"
    assert refs[0]["name"] == "run-owner-abc"
    assert refs[0]["uid"] == "cm-uid-123"
    assert refs[0]["blockOwnerDeletion"] is True


def test_manifest_without_owner_has_no_owner_references():
    rt = K8sSandboxRuntime()
    spec = SandboxLaunchSpec(image="img:dev", store_prefix="run/r/nodes/n")
    manifest = rt._manifest(spec, "sbx-test")
    assert "ownerReferences" not in manifest["metadata"]


def test_manifest_always_carries_sandbox_label():
    rt = K8sSandboxRuntime()
    spec = SandboxLaunchSpec(
        image="img:dev", store_prefix="run/r/nodes/n",
        labels={"resoluto.run_id": "abc", "resoluto.node_id": "n1"},
    )
    manifest = rt._manifest(spec, "sbx-test")
    assert manifest["metadata"]["labels"]["resoluto.sandbox"] == "true"
    assert manifest["metadata"]["labels"]["resoluto.run_id"] == "abc"


# ── NetworkPolicy: ConfigMap owner reference ─────────────────────────────────


def test_network_policy_with_configmap_owner():
    rt = K8sSandboxRuntime(egress=EgressConfig(store_cidr="10.0.0.1/32", llm_cidr="10.0.0.2/32"))
    spec = SandboxLaunchSpec(image="img:dev", store_prefix="run/r/nodes/n")
    policy = rt._network_policy(
        spec, "my-pod", "pod-uid",
        owner_name="run-owner-abc", owner_uid="cm-uid-123",
    )
    refs = policy["metadata"]["ownerReferences"]
    assert len(refs) == 1
    assert refs[0]["apiVersion"] == "v1"
    assert refs[0]["kind"] == "ConfigMap"
    assert refs[0]["name"] == "run-owner-abc"
    assert refs[0]["uid"] == "cm-uid-123"
    assert refs[0]["blockOwnerDeletion"] is True


# ── ResourceQuota and LimitRange manifests ───────────────────────────────────


def test_quota_manifest_defaults():
    rt = K8sSandboxRuntime()
    quota = rt._quota_manifest()
    assert quota["apiVersion"] == "v1"
    assert quota["kind"] == "ResourceQuota"
    assert quota["metadata"]["name"] == "resoluto-sandbox-quota"
    assert quota["spec"]["hard"]["pods"] == "20"
    assert quota["spec"]["hard"]["limits.memory"] == "96Gi"


def test_quota_manifest_env_override(monkeypatch):
    monkeypatch.setenv("RESOLUTO_SANDBOX_MAX_PODS", "50")
    monkeypatch.setenv("RESOLUTO_SANDBOX_MAX_MEMORY", "200Gi")
    rt = K8sSandboxRuntime()
    quota = rt._quota_manifest()
    assert quota["spec"]["hard"]["pods"] == "50"
    assert quota["spec"]["hard"]["limits.memory"] == "200Gi"


def test_limit_range_manifest_defaults():
    rt = K8sSandboxRuntime()
    lr = rt._limit_range_manifest()
    assert lr["apiVersion"] == "v1"
    assert lr["kind"] == "LimitRange"
    assert lr["metadata"]["name"] == "resoluto-sandbox-limits"
    limits = lr["spec"]["limits"]
    assert len(limits) == 1
    assert limits[0]["type"] == "Pod"
    assert limits[0]["max"]["memory"] == "24Gi"
    assert limits[0]["max"]["cpu"] == "4"


def test_limit_range_manifest_env_override(monkeypatch):
    monkeypatch.setenv("RESOLUTO_SANDBOX_POD_MAX_MEMORY", "48Gi")
    monkeypatch.setenv("RESOLUTO_SANDBOX_POD_MAX_CPU", "8")
    rt = K8sSandboxRuntime()
    lr = rt._limit_range_manifest()
    assert lr["spec"]["limits"][0]["max"]["memory"] == "48Gi"
    assert lr["spec"]["limits"][0]["max"]["cpu"] == "8"


# ── Runtime class admission guard (direct launch bypass protection) ───────────


@pytest.mark.parametrize("rc", ["", "runc"])
@pytest.mark.asyncio
async def test_launch_refuses_non_kata_without_flag(rc, monkeypatch):
    monkeypatch.delenv("RESOLUTO_TRUSTED_LOCAL", raising=False)
    rt = K8sSandboxRuntime()
    spec = SandboxLaunchSpec(image="img:dev", store_prefix="run/r/nodes/n", runtime_class=rc)
    with pytest.raises(RuntimeError, match="RESOLUTO_TRUSTED_LOCAL"):
        await rt.launch(spec)


@pytest.mark.parametrize("rc", ["", "runc"])
@pytest.mark.asyncio
async def test_launch_permits_non_kata_with_trusted_local_flag(rc, monkeypatch, caplog):
    monkeypatch.setenv("RESOLUTO_TRUSTED_LOCAL", "1")
    rt = K8sSandboxRuntime()
    spec = SandboxLaunchSpec(image="img:dev", store_prefix="run/r/nodes/n", runtime_class=rc)
    with caplog.at_level(logging.WARNING):
        try:
            await rt.launch(spec)
        except RuntimeError as exc:
            assert "RESOLUTO_TRUSTED_LOCAL" not in str(exc), f"Guard should not have blocked: {exc}"
        except Exception:
            pass  # expected: no k8s cluster in test environment
    assert any("trusted-local" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_launch_default_kata_passes_guard(monkeypatch):
    monkeypatch.delenv("RESOLUTO_TRUSTED_LOCAL", raising=False)
    rt = K8sSandboxRuntime()
    spec = SandboxLaunchSpec(image="img:dev", store_prefix="run/r/nodes/n")  # kata default
    try:
        await rt.launch(spec)
    except RuntimeError as exc:
        assert "RESOLUTO_TRUSTED_LOCAL" not in str(exc), f"Guard should not have blocked: {exc}"
    except Exception:
        pass  # expected: no k8s cluster in test environment
