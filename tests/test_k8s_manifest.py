"""The pod manifest must carry activeDeadlineSeconds ONLY when the spec sets one —
no hidden wall-clock deadline on lanes (liveness is the watchdog, not a timer)."""
import logging

import pytest

from resoluto_sandbox.contracts import Resources, SandboxLaunchSpec, parse_quantity
from resoluto_sandbox.runtime.k8s import EgressConfig, K8sSandboxRuntime


@pytest.fixture(autouse=True)
def _never_touch_a_real_cluster(monkeypatch):
    """These are UNIT tests of manifest/guard/preflight logic — they must never reach a
    real k8s API. Several call `await rt.launch(...)` expecting it to fail "because there's
    no cluster", but on a dev box with k3s reachable that assumption is false and launch
    leaks real `img:dev` pods (ImagePullBackOff forever). Stub `_client` so any API call
    raises instead of hitting the cluster — the guard/preflight asserts still hold."""
    async def _no_api(self):
        raise RuntimeError("unit test: k8s API access is stubbed out")
    monkeypatch.setattr(K8sSandboxRuntime, "_client", _no_api)


# ── docker graph backend ─────────────────────────────────────────────────────


def test_dind_tmpfs_emits_memory_medium():
    # graph_backend is now the runtime's private config (default tmpfs); the graph SIZE is a
    # neutral resource. k8s renders the size as raw bytes (a valid quantity).
    rt = K8sSandboxRuntime(graph_backend="tmpfs")
    spec = SandboxLaunchSpec(
        image="img:dev", store_prefix="run/r/nodes/n", flavor="dind",
        resources=Resources.from_quantities(memory="20Gi", cpu="2", dind_graph="16Gi"),
    )
    manifest = rt._manifest(spec, "sbx-test")
    graph_vol = next(v for v in manifest["spec"]["volumes"] if v["name"] == "docker-graph")
    assert graph_vol["emptyDir"]["medium"] == "Memory"
    assert graph_vol["emptyDir"]["sizeLimit"] == str(parse_quantity("16Gi"))


def test_dind_block_emits_no_medium():
    # block backend + its sizeLimit are k8s-runtime config now, not spec fields.
    rt = K8sSandboxRuntime(graph_backend="block", graph_block_size="50Gi")
    spec = SandboxLaunchSpec(
        image="img:dev", store_prefix="run/r/nodes/n", flavor="dind",
        resources=Resources.from_quantities(memory="20Gi", cpu="2", dind_graph="16Gi"),
    )
    manifest = rt._manifest(spec, "sbx-test")
    graph_vol = next(v for v in manifest["spec"]["volumes"] if v["name"] == "docker-graph")
    assert "medium" not in graph_vol["emptyDir"]
    assert graph_vol["emptyDir"]["sizeLimit"] == "50Gi"


def test_plain_flavor_has_no_docker_graph_volume():
    rt = K8sSandboxRuntime(graph_backend="block")
    spec = SandboxLaunchSpec(image="img:dev", store_prefix="run/r/nodes/n", flavor="plain")
    manifest = rt._manifest(spec, "sbx-test")
    graph_vols = [v for v in manifest["spec"]["volumes"] if v["name"] == "docker-graph"]
    assert graph_vols == []


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
    rt = K8sSandboxRuntime(egress=EgressConfig(store_cidr="10.0.0.1/32"))
    spec = SandboxLaunchSpec(image="img:dev", store_prefix="run/r/nodes/n", labels={"app": "lane"})
    policy = rt._network_policy(spec, "sbx-test", "fake-uid-123")
    assert policy["spec"]["policyTypes"] == ["Egress"]
    assert policy["kind"] == "NetworkPolicy"
    assert policy["apiVersion"] == "networking.k8s.io/v1"


def test_network_policy_exact_peers_store_https_dns():
    rt = K8sSandboxRuntime(egress=EgressConfig(store_cidr="10.0.0.1/32", store_port=9100))
    spec = SandboxLaunchSpec(image="img:dev", store_prefix="run/r/nodes/n")
    policy = rt._network_policy(spec, "sbx-test", "fake-uid")
    rules = policy["spec"]["egress"]
    assert len(rules) == 3
    # rule 0: the object store on store_port
    assert rules[0]["to"][0]["ipBlock"]["cidr"] == "10.0.0.1/32"
    assert rules[0]["ports"] == [{"port": 9100, "protocol": "TCP"}]
    # rule 1: public HTTPS to anywhere
    assert rules[1]["to"][0]["ipBlock"]["cidr"] == "0.0.0.0/0"
    assert rules[1]["ports"] == [{"port": 443, "protocol": "TCP"}]
    # rule 2: DNS (UDP + TCP) to anywhere
    assert rules[2]["to"][0]["ipBlock"]["cidr"] == "0.0.0.0/0"
    assert rules[2]["ports"] == [
        {"port": 53, "protocol": "UDP"},
        {"port": 53, "protocol": "TCP"},
    ]


def test_network_policy_imds_blocked_in_broad_rules():
    # IMDS excepted on the broad 0.0.0.0/0 rules; the store rule (specific /32) carries no except
    # (k8s rejects an except that isn't a strict subset of the cidr).
    rt = K8sSandboxRuntime(egress=EgressConfig(store_cidr="10.0.0.1/32"))
    spec = SandboxLaunchSpec(image="img:dev", store_prefix="run/r/nodes/n")
    rules = rt._network_policy(spec, "sbx-test", "fake-uid")["spec"]["egress"]
    assert "except" not in rules[0]["to"][0]["ipBlock"]
    for rule in rules[1:]:
        assert rule["to"][0]["ipBlock"]["except"] == ["169.254.169.254/32"]


def test_network_policy_config_driven():
    rt1 = K8sSandboxRuntime(egress=EgressConfig(store_cidr="192.168.1.100/32"))
    spec = SandboxLaunchSpec(image="img:dev", store_prefix="run/r/nodes/n")
    p1 = rt1._network_policy(spec, "sbx", "uid-1")
    assert p1["spec"]["egress"][0]["to"][0]["ipBlock"]["cidr"] == "192.168.1.100/32"

    rt2 = K8sSandboxRuntime(egress=EgressConfig(store_cidr="10.0.0.1/32", store_port=9100))
    p2 = rt2._network_policy(spec, "sbx", "uid-2")
    assert p2["spec"]["egress"][0]["ports"] == [{"port": 9100, "protocol": "TCP"}]
    assert len(p2["spec"]["egress"]) == 3


def test_network_policy_owner_reference():
    rt = K8sSandboxRuntime(egress=EgressConfig(store_cidr="10.0.0.1/32"))
    spec = SandboxLaunchSpec(image="img:dev", store_prefix="run/r/nodes/n")
    policy = rt._network_policy(spec, "my-pod", "my-pod-uid-456")
    refs = policy["metadata"]["ownerReferences"]
    assert len(refs) == 1
    assert refs[0]["kind"] == "Pod"
    assert refs[0]["name"] == "my-pod"
    assert refs[0]["uid"] == "my-pod-uid-456"
    assert refs[0]["blockOwnerDeletion"] is True


def test_egress_config_requires_cidr():
    with pytest.raises(ValueError, match="CIDR"):
        EgressConfig(store_cidr="api.anthropic.com")


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
    rt = K8sSandboxRuntime(egress=EgressConfig(store_cidr="10.0.0.1/32"))
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
async def test_launch_always_refuses_non_kata(rc, monkeypatch):
    # No bypass: a non-Kata runtime class is ALWAYS refused (RESOLUTO_TRUSTED_LOCAL is gone).
    monkeypatch.setenv("RESOLUTO_TRUSTED_LOCAL", "1")  # even a stale flag must not permit a downgrade
    rt = K8sSandboxRuntime(runtime_class=rc)  # runtime_class is the runtime's own config now
    spec = SandboxLaunchSpec(image="img:dev", store_prefix="run/r/nodes/n")
    with pytest.raises(RuntimeError, match="Isolation downgrade refused"):
        await rt.launch(spec)


# ── dind tmpfs memory preflight ──────────────────────────────────────────────


from resoluto_sandbox.runtime.k8s import _parse_k8s_memory  # noqa: E402


@pytest.mark.parametrize("s,expected", [
    ("1Ki", 1024),
    ("1Mi", 1024 ** 2),
    ("1Gi", 1024 ** 3),
    ("24Gi", 24 * 1024 ** 3),
    ("4096Mi", 4096 * 1024 ** 2),
    ("512", 512),
    ("2K", 2000),
    ("2M", 2_000_000),
    ("2G", 2_000_000_000),
    ("16Ti", 16 * 1024 ** 4),
])
def test_parse_k8s_memory_strings(s, expected):
    assert _parse_k8s_memory(s) == expected


def test_parse_k8s_memory_invalid():
    with pytest.raises(ValueError, match="Cannot parse"):
        _parse_k8s_memory("24xyz")


def _dind_spec(*, memory="24Gi", docker_graph_size="18Gi", **kwargs):
    # graph_backend (tmpfs default) is the K8s runtime's config; memory/graph are neutral resources.
    return SandboxLaunchSpec(
        image="img:dev", store_prefix="run/r/nodes/n", flavor="dind",
        resources=Resources.from_quantities(memory=memory, cpu="2", dind_graph=docker_graph_size),
        **kwargs,
    )


@pytest.mark.asyncio
async def test_dind_tmpfs_preflight_raises_when_graph_fills_pod():
    # graph == pod memory: nothing left for dockerd/build — check (a) must fire
    rt = K8sSandboxRuntime(node_allocatable_memory="32Gi")
    with pytest.raises(RuntimeError, match="graph does not fit inside pod"):
        await rt._preflight_memory(_dind_spec(memory="24Gi", docker_graph_size="24Gi"))


@pytest.mark.asyncio
async def test_dind_tmpfs_preflight_raises_when_graph_exceeds_pod():
    # graph > pod memory — check (a) must fire
    rt = K8sSandboxRuntime(node_allocatable_memory="32Gi")
    with pytest.raises(RuntimeError, match="graph does not fit inside pod"):
        await rt._preflight_memory(_dind_spec(memory="16Gi", docker_graph_size="20Gi"))


@pytest.mark.asyncio
async def test_dind_tmpfs_preflight_raises_when_pod_exceeds_node_allocatable():
    # pod > node allocatable: unschedulable — check (b) must fire
    rt = K8sSandboxRuntime(node_allocatable_memory="32Gi")
    with pytest.raises(RuntimeError, match="pod does not fit on node"):
        await rt._preflight_memory(_dind_spec(memory="36Gi", docker_graph_size="18Gi"))


@pytest.mark.asyncio
async def test_dind_tmpfs_preflight_passes_for_valid_k8s_profile():
    # 24Gi pod / 18Gi graph / 32Gi node — the RES-275..280 working profile must not raise
    rt = K8sSandboxRuntime(node_allocatable_memory="32Gi")
    await rt._preflight_memory(_dind_spec(memory="24Gi", docker_graph_size="18Gi"))


@pytest.mark.asyncio
async def test_dind_tmpfs_preflight_passes_when_pod_equals_node_allocatable():
    # pod == node allocatable: equal is allowed (pod is still schedulable)
    rt = K8sSandboxRuntime(node_allocatable_memory="24Gi")
    await rt._preflight_memory(_dind_spec(memory="24Gi", docker_graph_size="18Gi"))


@pytest.mark.asyncio
async def test_preflight_graph_error_message_names_env_var_knobs():
    # check (a) error: operator must know to shrink graph or switch to block backend
    rt = K8sSandboxRuntime(node_allocatable_memory="32Gi")
    with pytest.raises(RuntimeError) as exc_info:
        await rt._preflight_memory(_dind_spec(memory="10Gi", docker_graph_size="12Gi"))
    msg = str(exc_info.value)
    assert "RESOLUTO_LANE_DIND_GRAPH" in msg
    assert "RESOLUTO_LANE_DIND_MEMORY" in msg
    assert "block" in msg  # operator can switch to the block-backed graph


@pytest.mark.asyncio
async def test_preflight_node_error_message_names_env_var_knobs():
    # check (b) error: operator must know to shrink pod or provision a larger node
    rt = K8sSandboxRuntime(node_allocatable_memory="16Gi")
    with pytest.raises(RuntimeError) as exc_info:
        await rt._preflight_memory(_dind_spec(memory="24Gi", docker_graph_size="18Gi"))
    msg = str(exc_info.value)
    assert "RESOLUTO_LANE_DIND_MEMORY" in msg
    assert "pod does not fit on node" in msg


@pytest.mark.asyncio
async def test_preflight_env_var_overrides_node_query(monkeypatch):
    monkeypatch.setenv("RESOLUTO_NODE_ALLOCATABLE_MEMORY", "16Gi")
    rt = K8sSandboxRuntime()  # no constructor injection; env var is the source
    # pod 20Gi > node 16Gi → check (b) fires
    with pytest.raises(RuntimeError, match="pod does not fit on node"):
        await rt._preflight_memory(_dind_spec(memory="20Gi", docker_graph_size="8Gi"))


@pytest.mark.asyncio
async def test_preflight_fires_in_launch_for_dind_tmpfs(monkeypatch):
    monkeypatch.delenv("RESOLUTO_TRUSTED_LOCAL", raising=False)
    rt = K8sSandboxRuntime(node_allocatable_memory="8Gi")
    # pod 10Gi > node 8Gi → check (b) fires; graph 4Gi < pod 10Gi so check (a) does not fire
    spec = _dind_spec(memory="10Gi", docker_graph_size="4Gi")
    with pytest.raises(RuntimeError, match="pod does not fit on node"):
        await rt.launch(spec)


def test_manifest_stamps_opaque_scheduling_gates_and_annotations():
    """Decoupling contract: the substrate stamps caller-supplied scheduling gates +
    annotations VERBATIM (the seam Kueue composes through), and emits honest requests."""
    from resoluto_sandbox.contracts import SandboxLaunchSpec
    from resoluto_sandbox.runtime.k8s import K8sSandboxRuntime
    rt = K8sSandboxRuntime()
    spec = SandboxLaunchSpec(
        image="x", store_prefix="run/x/nodes/n/lane-0",
        labels={"kueue.x-k8s.io/queue-name": "team-a"},
        scheduling_gates=["kueue.x-k8s.io/admission"],
        annotations={"foo": "bar"},
    )
    m = rt._manifest(spec, "sbx-test")
    # gates relayed verbatim, never constructed by the substrate
    assert m["spec"]["schedulingGates"] == [{"name": "kueue.x-k8s.io/admission"}]
    assert m["metadata"]["annotations"] == {"foo": "bar"}
    assert m["metadata"]["labels"]["kueue.x-k8s.io/queue-name"] == "team-a"
    # honest requests == limits (right-sizing for any scheduler / quota layer)
    res = m["spec"]["containers"][0]["resources"]
    assert res["requests"]["memory"] == res["limits"]["memory"]


def test_manifest_no_gates_by_default_normal_scheduling():
    # No scheduling_gates → no schedulingGates key → plain kube-scheduler, no admitter.
    from resoluto_sandbox.contracts import SandboxLaunchSpec
    from resoluto_sandbox.runtime.k8s import K8sSandboxRuntime
    m = K8sSandboxRuntime()._manifest(
        SandboxLaunchSpec(image="x", store_prefix="run/x/nodes/n/lane-0"), "sbx-test")
    assert "schedulingGates" not in m["spec"]
    assert "annotations" not in m["metadata"]
