"""The runtime must PIN its kube-context, never follow the ambient current-context
(which can wander to an unrelated/production cluster and run adversarial lane pods)."""
import sys
import types

import pytest

from resoluto.sandbox.runtime.k8s import K8sSandboxRuntime


def test_constructor_stores_pinned_context():
    assert K8sSandboxRuntime(context="prod-cluster")._context == "prod-cluster"
    assert K8sSandboxRuntime()._context is None  # default = ambient (logged loud)


async def test_client_passes_pinned_context_to_load_kube_config(monkeypatch):
    recorded: dict = {}

    async def _load_kube_config(config_file=None, context=None):
        recorded["config_file"] = config_file
        recorded["context"] = context

    class _Cfg:
        host = "https://local-k3s:6443"

    class _CoreV1Api:
        api_client = types.SimpleNamespace(configuration=_Cfg())

    fake_config = types.SimpleNamespace(
        load_kube_config=_load_kube_config,
        load_incluster_config=lambda: None,
    )
    fake_client = types.SimpleNamespace(CoreV1Api=lambda: _CoreV1Api())
    fake_ka = types.SimpleNamespace(client=fake_client, config=fake_config)
    monkeypatch.setitem(sys.modules, "kubernetes_asyncio", fake_ka)
    monkeypatch.setitem(sys.modules, "kubernetes_asyncio.client", fake_client)
    monkeypatch.setitem(sys.modules, "kubernetes_asyncio.config", fake_config)

    rt = K8sSandboxRuntime(context="local-k3s", kubeconfig="/tmp/kc")

    async def _noop():
        return None

    monkeypatch.setattr(rt, "_ensure_namespace", _noop)

    await rt._client()
    assert recorded["context"] == "local-k3s"   # pinned, not the ambient current-context
    assert recorded["config_file"] == "/tmp/kc"
