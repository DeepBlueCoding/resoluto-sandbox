"""Hermetic unit tests for the PURE k8s lifecycle logic that today is only reachable
behind @integration. They NEVER reach a real cluster or launch a real pod: the `_client`
seam is stubbed with `monkeypatch.setattr(rt, "_client", ...)` so no kubernetes API is
touched. They run in the default `uv run pytest` path (NOT marked @integration)."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from kubernetes_asyncio.client.exceptions import ApiException

from resoluto.sandbox.contracts import SandboxHandle
from resoluto.sandbox.runtime.k8s import K8sSandboxRuntime, _dns_safe


def _stub_client(rt: K8sSandboxRuntime, api, monkeypatch) -> None:
    """Replace the awaited `_client()` seam so no real kubernetes API is reached."""

    async def _client():
        return api

    monkeypatch.setattr(rt, "_client", _client)


def _node(memory: str, *, ready: bool = True) -> SimpleNamespace:
    cond = SimpleNamespace(type="Ready", status="True" if ready else "False")
    return SimpleNamespace(
        status=SimpleNamespace(conditions=[cond], allocatable={"memory": memory})
    )


# ── _get_node_allocatable_ram: live-node branch (min across Ready nodes) ──────


@pytest.mark.asyncio
async def test_node_allocatable_ram_is_min_across_ready_nodes(monkeypatch):
    monkeypatch.delenv("RESOLUTO_NODE_ALLOCATABLE_MEMORY", raising=False)
    rt = K8sSandboxRuntime(node_allocatable_memory=None)

    nodes = SimpleNamespace(items=[_node("32Gi"), _node("16Gi"), _node("64Gi")])

    class _Api:
        async def list_node(self):
            return nodes

    _stub_client(rt, _Api(), monkeypatch)
    assert await rt._get_node_allocatable_ram() == 16 * 1024**3


@pytest.mark.asyncio
async def test_node_allocatable_ram_excludes_non_ready_node(monkeypatch):
    monkeypatch.delenv("RESOLUTO_NODE_ALLOCATABLE_MEMORY", raising=False)
    rt = K8sSandboxRuntime(node_allocatable_memory=None)

    # The non-Ready node carries the smallest RAM (8Gi) — it must be excluded, so the
    # min over the two Ready nodes is 16Gi, not 8Gi.
    nodes = SimpleNamespace(items=[_node("8Gi", ready=False), _node("32Gi"), _node("16Gi")])

    class _Api:
        async def list_node(self):
            return nodes

    _stub_client(rt, _Api(), monkeypatch)
    assert await rt._get_node_allocatable_ram() == 16 * 1024**3


@pytest.mark.asyncio
async def test_node_allocatable_ram_zero_when_no_nodes(monkeypatch):
    monkeypatch.delenv("RESOLUTO_NODE_ALLOCATABLE_MEMORY", raising=False)
    rt = K8sSandboxRuntime(node_allocatable_memory=None)

    class _Api:
        async def list_node(self):
            return SimpleNamespace(items=[])

    _stub_client(rt, _Api(), monkeypatch)
    assert await rt._get_node_allocatable_ram() == 0


@pytest.mark.asyncio
async def test_node_allocatable_ram_zero_when_none_ready(monkeypatch):
    monkeypatch.delenv("RESOLUTO_NODE_ALLOCATABLE_MEMORY", raising=False)
    rt = K8sSandboxRuntime(node_allocatable_memory=None)

    nodes = SimpleNamespace(items=[_node("32Gi", ready=False), _node("16Gi", ready=False)])

    class _Api:
        async def list_node(self):
            return nodes

    _stub_client(rt, _Api(), monkeypatch)
    assert await rt._get_node_allocatable_ram() == 0


# ── status(): phase map + terminated/waiting reason + 404 ─────────────────────


def _pod_status(*, phase, reason=None, container_statuses=None) -> SimpleNamespace:
    return SimpleNamespace(
        status=SimpleNamespace(phase=phase, reason=reason, container_statuses=container_statuses)
    )


@pytest.mark.asyncio
async def test_status_running_maps_phase(monkeypatch):
    rt = K8sSandboxRuntime()
    pod = _pod_status(phase="Running")

    class _Api:
        async def read_namespaced_pod(self, name, namespace):
            return pod

    _stub_client(rt, _Api(), monkeypatch)
    status = await rt.status(SandboxHandle(id="resoluto-sandboxes/sbx-1"))
    assert status.phase == "running"
    assert status.reason == ""
    assert status.exit_code is None


@pytest.mark.asyncio
async def test_status_reads_terminated_exit_code_and_reason(monkeypatch):
    rt = K8sSandboxRuntime()
    cs = SimpleNamespace(
        state=SimpleNamespace(
            terminated=SimpleNamespace(exit_code=137, reason="OOMKilled"),
            waiting=None,
        )
    )
    pod = _pod_status(phase="Failed", reason=None, container_statuses=[cs])

    class _Api:
        async def read_namespaced_pod(self, name, namespace):
            return pod

    _stub_client(rt, _Api(), monkeypatch)
    status = await rt.status(SandboxHandle(id="resoluto-sandboxes/sbx-1"))
    assert status.phase == "failed"
    assert status.exit_code == 137
    assert status.reason == "OOMKilled"


@pytest.mark.asyncio
async def test_status_existing_pod_reason_wins_over_terminated_reason(monkeypatch):
    rt = K8sSandboxRuntime()
    cs = SimpleNamespace(
        state=SimpleNamespace(
            terminated=SimpleNamespace(exit_code=1, reason="Error"),
            waiting=None,
        )
    )
    pod = _pod_status(phase="Failed", reason="Evicted", container_statuses=[cs])

    class _Api:
        async def read_namespaced_pod(self, name, namespace):
            return pod

    _stub_client(rt, _Api(), monkeypatch)
    status = await rt.status(SandboxHandle(id="resoluto-sandboxes/sbx-1"))
    # `reason or (term.reason or "")` — the pre-existing pod reason wins.
    assert status.reason == "Evicted"
    assert status.exit_code == 1


@pytest.mark.asyncio
async def test_status_reads_waiting_reason(monkeypatch):
    rt = K8sSandboxRuntime()
    cs = SimpleNamespace(
        state=SimpleNamespace(
            terminated=None,
            waiting=SimpleNamespace(reason="ImagePullBackOff"),
        )
    )
    pod = _pod_status(phase="Pending", reason=None, container_statuses=[cs])

    class _Api:
        async def read_namespaced_pod(self, name, namespace):
            return pod

    _stub_client(rt, _Api(), monkeypatch)
    status = await rt.status(SandboxHandle(id="resoluto-sandboxes/sbx-1"))
    assert status.phase == "pending"
    assert status.reason == "ImagePullBackOff"
    assert status.exit_code is None


@pytest.mark.asyncio
async def test_status_404_returns_unknown_pod_not_found(monkeypatch):
    rt = K8sSandboxRuntime()

    class _Api:
        async def read_namespaced_pod(self, name, namespace):
            raise ApiException(status=404)

    _stub_client(rt, _Api(), monkeypatch)
    status = await rt.status(SandboxHandle(id="resoluto-sandboxes/sbx-1"))
    assert status.phase == "unknown"
    assert status.reason == "pod not found"


@pytest.mark.asyncio
async def test_status_non_404_reraises(monkeypatch):
    rt = K8sSandboxRuntime()

    class _Api:
        async def read_namespaced_pod(self, name, namespace):
            raise ApiException(status=500)

    _stub_client(rt, _Api(), monkeypatch)
    with pytest.raises(ApiException):
        await rt.status(SandboxHandle(id="resoluto-sandboxes/sbx-1"))


# ── ensure_run_owner(): create, and 409 → read existing uid ───────────────────


@pytest.mark.asyncio
async def test_ensure_run_owner_creates_and_returns_name_uid(monkeypatch):
    rt = K8sSandboxRuntime()

    class _Api:
        async def create_namespaced_config_map(self, namespace, body):
            return SimpleNamespace(metadata=SimpleNamespace(uid="fresh-uid-1"))

    _stub_client(rt, _Api(), monkeypatch)
    name, uid = await rt.ensure_run_owner("RES-42")
    assert name == f"run-owner-{_dns_safe('RES-42')}"
    assert name == "run-owner-res-42"
    assert uid == "fresh-uid-1"


@pytest.mark.asyncio
async def test_ensure_run_owner_409_reads_existing_uid(monkeypatch):
    rt = K8sSandboxRuntime()

    class _Api:
        async def create_namespaced_config_map(self, namespace, body):
            raise ApiException(status=409)

        async def read_namespaced_config_map(self, name, namespace):
            return SimpleNamespace(metadata=SimpleNamespace(uid="existing-uid-9"))

    _stub_client(rt, _Api(), monkeypatch)
    name, uid = await rt.ensure_run_owner("RES-42")
    assert name == "run-owner-res-42"
    assert uid == "existing-uid-9"


# ── reap_stale_run_owners(): skip kept + young, delete old ────────────────────


def _cm(run_id: str, created: datetime | None) -> SimpleNamespace:
    return SimpleNamespace(
        metadata=SimpleNamespace(labels={"resoluto.run_id": run_id}, creation_timestamp=created)
    )


@pytest.mark.asyncio
async def test_reap_deletes_only_old_non_kept(monkeypatch):
    rt = K8sSandboxRuntime()
    old = datetime(2000, 1, 1, tzinfo=UTC)
    young = datetime.now(UTC)
    deleted: list[str] = []

    cms = SimpleNamespace(
        items=[
            _cm("keep-me", old),  # kept → skipped despite being old
            _cm("young-one", young),  # too young → skipped
            _cm("old-stale", old),  # old + not kept → deleted
        ]
    )

    class _Api:
        async def list_namespaced_config_map(self, namespace, label_selector):
            assert label_selector == "resoluto.run_id"
            return cms

        async def delete_namespaced_config_map(self, name, namespace):
            deleted.append(name)

    _stub_client(rt, _Api(), monkeypatch)
    n = await rt.reap_stale_run_owners(keep_run_id="keep-me", max_age_s=7200.0)
    assert n == 1
    assert deleted == ["run-owner-old-stale"]


# ── count_active_pods(): exclude Succeeded/Failed, kind filter ────────────────


def _pod(phase: str) -> SimpleNamespace:
    return SimpleNamespace(status=SimpleNamespace(phase=phase))


@pytest.mark.asyncio
async def test_count_active_pods_excludes_terminal(monkeypatch):
    rt = K8sSandboxRuntime()
    pods = SimpleNamespace(
        items=[
            _pod("Running"),
            _pod("Pending"),
            _pod("Succeeded"),  # terminal → excluded
            _pod("Failed"),  # terminal → excluded
            _pod("Unknown"),
        ]
    )

    class _Api:
        async def list_namespaced_pod(self, namespace, label_selector):
            assert label_selector == "resoluto_sandbox=true"
            return pods

    _stub_client(rt, _Api(), monkeypatch)
    assert await rt.count_active_pods() == 3


@pytest.mark.asyncio
async def test_count_active_pods_kind_filter_adds_label(monkeypatch):
    rt = K8sSandboxRuntime()
    seen: dict[str, str] = {}
    pods = SimpleNamespace(items=[_pod("Running"), _pod("Succeeded")])

    class _Api:
        async def list_namespaced_pod(self, namespace, label_selector):
            seen["selector"] = label_selector
            return pods

    _stub_client(rt, _Api(), monkeypatch)
    n = await rt.count_active_pods(kind="pool_a")
    assert n == 1
    assert seen["selector"] == "resoluto_sandbox=true,resoluto.kind=pool_a"
