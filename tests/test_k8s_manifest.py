"""The pod manifest must carry activeDeadlineSeconds ONLY when the spec sets one —
no hidden wall-clock deadline on sandboxes (liveness is the watchdog, not a timer)."""

import logging

import pytest

from resoluto.sandbox.contracts import Resources, SandboxLaunchSpec, parse_quantity
from resoluto.sandbox.runtime.k8s import EgressConfig, K8sSandboxRuntime


@pytest.fixture(autouse=True)
def _never_touch_a_real_cluster(monkeypatch):
    """These are UNIT tests of manifest/guard/preflight logic — they must never reach a
    real k8s API. Several call `await rt.launch(...)` expecting it to fail "because there's
    no cluster", but on a dev box with k3s reachable that assumption is false and launch
    leaks real `img:0.1.0` pods (ImagePullBackOff forever). Stub `_client` so any API call
    raises instead of hitting the cluster — the guard/preflight asserts still hold."""

    async def _no_api(self):
        raise RuntimeError("unit test: k8s API access is stubbed out")

    monkeypatch.setattr(K8sSandboxRuntime, "_client", _no_api)


# ── docker graph backend ─────────────────────────────────────────────────────


def test_dind_tmpfs_emits_memory_medium():
    # graph_backend is now a NEUTRAL per-step field on Resources (default tmpfs); the graph SIZE
    # is a neutral resource. tmpfs → medium:Memory emptyDir, sizeLimit = the graph bytes.
    rt = K8sSandboxRuntime()
    spec = SandboxLaunchSpec(
        image="img:0.1.0",
        store_prefix="run/r/nodes/n",
        flavor="dind",
        resources=Resources.from_quantities(
            memory="20Gi", cpu="2", dind_graph="16Gi", graph_backend="tmpfs"
        ),
    )
    manifest = rt._manifest(spec, "sbx-test")
    graph_vol = next(v for v in manifest["spec"]["volumes"] if v["name"] == "docker-graph")
    assert graph_vol["emptyDir"]["medium"] == "Memory"
    assert graph_vol["emptyDir"]["sizeLimit"] == str(parse_quantity("16Gi"))


def test_dind_tmpfs_omits_sizelimit_when_graph_unset():
    # A dind spec with no dind_graph size must NOT render sizeLimit:"None" (the literal string),
    # which k8s rejects as an invalid quantity (BadRequest). Omit it instead.
    rt = K8sSandboxRuntime()
    spec = SandboxLaunchSpec(image="img:0.1.0", store_prefix="run/r/nodes/n", flavor="dind")
    graph_vol = next(
        v for v in rt._manifest(spec, "sbx")["spec"]["volumes"] if v["name"] == "docker-graph"
    )
    assert graph_vol["emptyDir"]["medium"] == "Memory"
    assert "sizeLimit" not in graph_vol["emptyDir"]  # never the string "None"


def test_dind_block_emits_no_medium():
    # graph_backend=block is a NEUTRAL spec field now (both runtimes honor it). block → a
    # disk-backed emptyDir (no medium:Memory); sizeLimit = the graph DISK size from the spec.
    rt = K8sSandboxRuntime()
    spec = SandboxLaunchSpec(
        image="img:0.1.0",
        store_prefix="run/r/nodes/n",
        flavor="dind",
        resources=Resources.from_quantities(
            memory="6Gi", cpu="2", dind_graph="20Gi", graph_backend="block"
        ),
    )
    manifest = rt._manifest(spec, "sbx-test")
    graph_vol = next(v for v in manifest["spec"]["volumes"] if v["name"] == "docker-graph")
    assert "medium" not in graph_vol["emptyDir"]  # disk-backed → RAM stays free
    assert graph_vol["emptyDir"]["sizeLimit"] == str(parse_quantity("20Gi"))


def test_plain_flavor_has_no_docker_graph_volume():
    rt = K8sSandboxRuntime()
    spec = SandboxLaunchSpec(image="img:0.1.0", store_prefix="run/r/nodes/n", flavor="plain")
    manifest = rt._manifest(spec, "sbx-test")
    graph_vols = [v for v in manifest["spec"]["volumes"] if v["name"] == "docker-graph"]
    assert graph_vols == []


def test_manifest_omits_active_deadline_when_none():
    rt = K8sSandboxRuntime()
    spec = SandboxLaunchSpec(image="img:0.1.0", store_prefix="run/r/nodes/n")
    manifest = rt._manifest(spec, "sbx-test")
    assert "activeDeadlineSeconds" not in manifest["spec"]

    capped = SandboxLaunchSpec(
        image="img:0.1.0", store_prefix="run/r/nodes/n", deadline_seconds=900
    )
    manifest_capped = rt._manifest(capped, "sbx-test")
    assert manifest_capped["spec"]["activeDeadlineSeconds"] == 900


# ── NetworkPolicy tests ──────────────────────────────────────────────────────


def test_network_policy_default_deny_egress():
    rt = K8sSandboxRuntime(egress=EgressConfig(store_cidr="10.0.0.1/32"))
    spec = SandboxLaunchSpec(
        image="img:0.1.0", store_prefix="run/r/nodes/n", labels={"app": "pool_a"}
    )
    policy = rt._network_policy(spec, "sbx-test", "fake-uid-123")
    assert policy["spec"]["policyTypes"] == ["Egress"]
    assert policy["kind"] == "NetworkPolicy"
    assert policy["apiVersion"] == "networking.k8s.io/v1"
    # SECURE BY DEFAULT: no blanket public 0.0.0.0/0:443 rule unless public_https=True is opted in
    assert not any(
        r["ports"] == [{"port": 443, "protocol": "TCP"}]
        and r["to"][0]["ipBlock"]["cidr"] == "0.0.0.0/0"
        for r in policy["spec"]["egress"]
    )


def test_network_policy_exact_peers_store_https_dns():
    # public_https is graph-declared → carried on the SPEC; the runtime holds only the store base.
    rt = K8sSandboxRuntime(egress=EgressConfig(store_cidr="10.0.0.1/32", store_port=9100))
    spec = SandboxLaunchSpec(
        image="img:0.1.0",
        store_prefix="run/r/nodes/n",
        egress_public_https=True,
        labels={"resoluto.run_id": "r", "resoluto.node_id": "n"},
    )
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
    spec = SandboxLaunchSpec(
        image="img:0.1.0",
        store_prefix="run/r/nodes/n",
        egress_public_https=True,
        labels={"resoluto.run_id": "r", "resoluto.node_id": "n"},
    )
    rules = rt._network_policy(spec, "sbx-test", "fake-uid")["spec"]["egress"]
    assert "except" not in rules[0]["to"][0]["ipBlock"]
    for rule in rules[1:]:
        assert rule["to"][0]["ipBlock"]["except"] == ["169.254.169.254/32"]


def test_network_policy_config_driven():
    rt1 = K8sSandboxRuntime(egress=EgressConfig(store_cidr="192.168.1.100/32"))
    spec = SandboxLaunchSpec(
        image="img:0.1.0",
        store_prefix="run/r/nodes/n",
        labels={"resoluto.run_id": "r", "resoluto.node_id": "n"},
    )
    p1 = rt1._network_policy(spec, "sbx", "uid-1")
    assert p1["spec"]["egress"][0]["to"][0]["ipBlock"]["cidr"] == "192.168.1.100/32"

    rt2 = K8sSandboxRuntime(egress=EgressConfig(store_cidr="10.0.0.1/32", store_port=9100))
    spec2 = SandboxLaunchSpec(
        image="img:0.1.0",
        store_prefix="run/r/nodes/n",
        egress_public_https=True,
        labels={"resoluto.run_id": "r", "resoluto.node_id": "n"},
    )
    p2 = rt2._network_policy(spec2, "sbx", "uid-2")
    assert p2["spec"]["egress"][0]["ports"] == [{"port": 9100, "protocol": "TCP"}]
    assert len(p2["spec"]["egress"]) == 3


def test_network_policy_egress_policy_comes_from_the_spec():
    # The sandbox APPLIES the graph-declared egress carried on the SPEC (not the runtime's env).
    # store base stays the runtime's; allow + public_https come from the spec, per step.
    rt = K8sSandboxRuntime(egress=EgressConfig(store_cidr="10.0.0.1/32", store_port=9100))
    # spec opts into a specific host on 443 (no public_https)
    spec = SandboxLaunchSpec(
        image="img:0.1.0",
        store_prefix="run/r/nodes/n",
        egress_allow=["10.20.30.40/32"],
        egress_public_https=False,
        labels={"resoluto.run_id": "r", "resoluto.node_id": "n"},
    )
    rules = rt._network_policy(spec, "sbx", "uid")["spec"]["egress"]
    cidrs = [r["to"][0]["ipBlock"]["cidr"] for r in rules]
    assert "10.20.30.40/32" in cidrs  # the spec's allow host is opened
    assert "10.0.0.1/32" in cidrs  # store base still present (runtime infra)
    # no blanket public 443 because the spec did not set public_https
    assert not any(
        c == "0.0.0.0/0" and {"port": 443, "protocol": "TCP"} in r["ports"]
        for r, c in zip(rules, cidrs)
    )


def test_network_policy_owner_reference():
    rt = K8sSandboxRuntime(egress=EgressConfig(store_cidr="10.0.0.1/32"))
    spec = SandboxLaunchSpec(
        image="img:0.1.0",
        store_prefix="run/r/nodes/n",
        labels={"resoluto.run_id": "r", "resoluto.node_id": "n"},
    )
    policy = rt._network_policy(spec, "my-pod", "my-pod-uid-456")
    refs = policy["metadata"]["ownerReferences"]
    assert len(refs) == 1
    assert refs[0]["kind"] == "Pod"
    assert refs[0]["name"] == "my-pod"
    assert refs[0]["uid"] == "my-pod-uid-456"
    assert refs[0]["blockOwnerDeletion"] is True


def test_network_policy_refuses_empty_labels():
    # An empty podSelector would apply this pod's egress rules to EVERY pod in the namespace —
    # refuse rather than silently widen egress. The launcher always sets run_id/node_id labels.
    rt = K8sSandboxRuntime(egress=EgressConfig(store_cidr="10.0.0.1/32"))
    spec = SandboxLaunchSpec(image="img:0.1.0", store_prefix="run/r/nodes/n")  # no labels
    with pytest.raises(RuntimeError, match="no labels"):
        rt._network_policy(spec, "sbx", "uid")


def test_egress_config_requires_cidr():
    with pytest.raises(ValueError, match="CIDR"):
        EgressConfig(store_cidr="api.anthropic.com")


# ── ownerReferences on pod manifest ─────────────────────────────────────────


def test_manifest_with_owner_has_configmap_owner_reference():
    rt = K8sSandboxRuntime()
    spec = SandboxLaunchSpec(image="img:0.1.0", store_prefix="run/r/nodes/n")
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
    spec = SandboxLaunchSpec(image="img:0.1.0", store_prefix="run/r/nodes/n")
    manifest = rt._manifest(spec, "sbx-test")
    assert "ownerReferences" not in manifest["metadata"]


def test_manifest_always_carries_sandbox_label():
    rt = K8sSandboxRuntime()
    spec = SandboxLaunchSpec(
        image="img:0.1.0",
        store_prefix="run/r/nodes/n",
        labels={"resoluto.run_id": "abc", "resoluto.node_id": "n1"},
    )
    manifest = rt._manifest(spec, "sbx-test")
    assert manifest["metadata"]["labels"]["resoluto_sandbox"] == "true"
    assert manifest["metadata"]["labels"]["resoluto.run_id"] == "abc"


# ── NetworkPolicy: ConfigMap owner reference ─────────────────────────────────


def test_network_policy_with_configmap_owner():
    rt = K8sSandboxRuntime(egress=EgressConfig(store_cidr="10.0.0.1/32"))
    spec = SandboxLaunchSpec(
        image="img:0.1.0",
        store_prefix="run/r/nodes/n",
        labels={"resoluto.run_id": "r", "resoluto.node_id": "n"},
    )
    policy = rt._network_policy(
        spec,
        "my-pod",
        "pod-uid",
        owner_name="run-owner-abc",
        owner_uid="cm-uid-123",
    )
    refs = policy["metadata"]["ownerReferences"]
    assert len(refs) == 1
    assert refs[0]["apiVersion"] == "v1"
    assert refs[0]["kind"] == "ConfigMap"
    assert refs[0]["name"] == "run-owner-abc"
    assert refs[0]["uid"] == "cm-uid-123"
    assert refs[0]["blockOwnerDeletion"] is True


# The sandbox no longer declares cluster resource policy (ResourceQuota / LimitRange). The
# whole-cluster budget + per-pod caps are the ENGINE's concern (its admission pool + the
# operator-provisioned Kueue ClusterQueue); the sandbox only applies the per-launch limits it's
# handed via SandboxLaunchSpec.resources (proven by test_pod_manifest_* above).


# ── Runtime class admission guard (direct launch bypass protection) ───────────


@pytest.mark.parametrize("rc", ["", "runc"])
@pytest.mark.asyncio
async def test_launch_always_refuses_non_kata(rc, monkeypatch):
    # No bypass: a non-Kata runtime class is ALWAYS refused (RESOLUTO_TRUSTED_LOCAL is gone).
    monkeypatch.setenv(
        "RESOLUTO_TRUSTED_LOCAL", "1"
    )  # even a stale flag must not permit a downgrade
    rt = K8sSandboxRuntime(runtime_class=rc)  # runtime_class is the runtime's own config now
    spec = SandboxLaunchSpec(image="img:0.1.0", store_prefix="run/r/nodes/n")
    with pytest.raises(RuntimeError, match="Isolation downgrade refused"):
        await rt.launch(spec)


# ── dind tmpfs memory preflight ──────────────────────────────────────────────


from resoluto.sandbox.runtime.k8s import _parse_k8s_memory  # noqa: E402


@pytest.mark.parametrize(
    "s,expected",
    [
        ("1Ki", 1024),
        ("1Mi", 1024**2),
        ("1Gi", 1024**3),
        ("24Gi", 24 * 1024**3),
        ("4096Mi", 4096 * 1024**2),
        ("512", 512),
        ("2K", 2000),
        ("2M", 2_000_000),
        ("2G", 2_000_000_000),
        ("16Ti", 16 * 1024**4),
    ],
)
def test_parse_k8s_memory_strings(s, expected):
    assert _parse_k8s_memory(s) == expected


def test_parse_k8s_memory_invalid():
    with pytest.raises(ValueError, match="Cannot parse"):
        _parse_k8s_memory("24xyz")


def _dind_spec(*, memory="24Gi", docker_graph_size="18Gi", **kwargs):
    # graph_backend (tmpfs default) is the K8s runtime's config; memory/graph are neutral resources.
    return SandboxLaunchSpec(
        image="img:0.1.0",
        store_prefix="run/r/nodes/n",
        flavor="dind",
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
async def test_preflight_graph_error_message_names_the_knobs():
    # check (a) error: operator must know to shrink graph or switch to block backend
    rt = K8sSandboxRuntime(node_allocatable_memory="32Gi")
    with pytest.raises(RuntimeError) as exc_info:
        await rt._preflight_memory(_dind_spec(memory="10Gi", docker_graph_size="12Gi"))
    msg = str(exc_info.value)
    assert "graph does not fit inside pod" in msg
    assert "dind graph size" in msg
    assert "block" in msg  # operator can switch to the block-backed graph


@pytest.mark.asyncio
async def test_preflight_node_error_message_names_the_knobs():
    # check (b) error: operator must know to shrink pod or provision a larger node
    rt = K8sSandboxRuntime(node_allocatable_memory="16Gi")
    with pytest.raises(RuntimeError) as exc_info:
        await rt._preflight_memory(_dind_spec(memory="24Gi", docker_graph_size="18Gi"))
    msg = str(exc_info.value)
    assert "dind pod memory" in msg
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
    from resoluto.sandbox.contracts import SandboxLaunchSpec
    from resoluto.sandbox.runtime.k8s import K8sSandboxRuntime

    rt = K8sSandboxRuntime()
    spec = SandboxLaunchSpec(
        image="x",
        store_prefix="run/x/nodes/n/sbx-0",
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
    from resoluto.sandbox.contracts import SandboxLaunchSpec
    from resoluto.sandbox.runtime.k8s import K8sSandboxRuntime

    m = K8sSandboxRuntime()._manifest(
        SandboxLaunchSpec(image="x", store_prefix="run/x/nodes/n/sbx-0"), "sbx-test"
    )
    assert "schedulingGates" not in m["spec"]
    assert "annotations" not in m["metadata"]


# ── k8s_secret_refs → valueFrom.secretKeyRef ─────────────────────────────────


def test_manifest_renders_k8s_secret_refs_as_valuefrom():
    rt = K8sSandboxRuntime()
    spec = SandboxLaunchSpec(
        image="img:0.1.0",
        store_prefix="run/r/nodes/n",
        env={"PLAIN": "value"},
        k8s_secret_refs={"ANTHROPIC_API_KEY": ("anthropic-key", "api_key")},
    )
    env = rt._manifest(spec, "sbx-test")["spec"]["containers"][0]["env"]
    plain = next(e for e in env if e["name"] == "PLAIN")
    assert plain == {"name": "PLAIN", "value": "value"}
    secret = next(e for e in env if e["name"] == "ANTHROPIC_API_KEY")
    assert secret == {
        "name": "ANTHROPIC_API_KEY",
        "valueFrom": {"secretKeyRef": {"name": "anthropic-key", "key": "api_key"}},
    }
    assert "value" not in secret  # never a literal alongside the ref


def test_manifest_omits_secret_refs_when_none_declared():
    rt = K8sSandboxRuntime()
    spec = SandboxLaunchSpec(image="img:0.1.0", store_prefix="run/r/nodes/n")
    env = rt._manifest(spec, "sbx-test")["spec"]["containers"][0]["env"]
    assert all("valueFrom" not in e for e in env)


def test_priority_class_relayed_when_set():
    rt = K8sSandboxRuntime()
    spec = SandboxLaunchSpec(
        image="img:0.1.0",
        store_prefix="run/r/nodes/n",
        priority_class="resoluto-sandbox-low",
    )
    manifest = rt._manifest(spec, "sbx-test")
    assert manifest["spec"]["priorityClassName"] == "resoluto-sandbox-low"


def test_no_priority_class_field_when_unset():
    rt = K8sSandboxRuntime()
    spec = SandboxLaunchSpec(image="img:0.1.0", store_prefix="run/r/nodes/n")
    manifest = rt._manifest(spec, "sbx-test")
    assert "priorityClassName" not in manifest["spec"]
