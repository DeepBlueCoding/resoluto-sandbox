"""Launch-time API failures: 5xx/429 (cluster hiccup, admission-webhook blip) become the
typed transient SandboxLaunchError; 4xx config errors stay raw and loud. Real incident:
a crashlooping Kueue webhook 502'd pod creation and crashed a whole pipeline."""

import pytest
from kubernetes_asyncio.client.exceptions import ApiException

from resoluto.sandbox.contracts import Resources, SandboxLaunchError, SandboxLaunchSpec
from resoluto.sandbox.runtime.k8s import K8sSandboxRuntime


def _spec() -> SandboxLaunchSpec:
    return SandboxLaunchSpec(
        image="img:0.1.0",
        store_prefix="run/r/nodes/n/sandbox-0",
        resources=Resources.from_quantities(memory="1Gi", cpu="1"),
        labels={"resoluto.run_id": "", "resoluto.node_id": "n"},
    )


class _FailingApi:
    def __init__(self, status: int):
        self._status = status

    async def create_namespaced_pod(self, namespace, body):
        raise ApiException(status=self._status, reason="Internal Server Error")


def _runtime(monkeypatch, status: int) -> K8sSandboxRuntime:
    rt = K8sSandboxRuntime(context="unit-test")
    api = _FailingApi(status)

    async def _client(self):
        return api

    monkeypatch.setattr(K8sSandboxRuntime, "_client", _client)
    return rt


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [500, 502, 503, 429])
async def test_transient_api_errors_become_the_typed_launch_error(monkeypatch, status):
    rt = _runtime(monkeypatch, status)
    with pytest.raises(SandboxLaunchError, match=str(status)):
        await rt.launch(_spec())


@pytest.mark.asyncio
async def test_config_errors_stay_raw_and_loud(monkeypatch):
    rt = _runtime(monkeypatch, 403)
    with pytest.raises(ApiException):
        await rt.launch(_spec())
