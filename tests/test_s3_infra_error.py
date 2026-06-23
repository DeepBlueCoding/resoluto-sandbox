"""S3ObjectStore translates transport failures (e.g. minio storage-full) into a
typed ObjectStoreError so the engine can fail the run fast with the real cause."""
import pytest

from resoluto_sandbox.contracts import ObjectStoreError
from resoluto_sandbox.objectstore.s3 import S3ObjectStore


class _FakeClient:
    def __init__(self, exc):
        self._exc = exc

    async def put_object(self, **kw):
        raise self._exc


class _FakeCM:
    def __init__(self, exc):
        self._exc = exc

    async def __aenter__(self):
        return _FakeClient(self._exc)

    async def __aexit__(self, *a):
        return False


@pytest.mark.asyncio
async def test_put_wraps_clienterror_as_infrastructure_error(monkeypatch):
    from botocore.exceptions import ClientError
    exc = ClientError(
        {"Error": {"Code": "XMinioStorageFull", "Message": "min free drive threshold"}},
        "PutObject",
    )
    store = S3ObjectStore("lanes")
    monkeypatch.setattr(store, "_client", lambda: _FakeCM(exc))

    with pytest.raises(ObjectStoreError) as ei:
        await store.put("k", b"data")
    assert "object store I/O failed" in str(ei.value)
    assert "XMinioStorageFull" in str(ei.value)


@pytest.mark.asyncio
async def test_put_wraps_connection_error_as_infrastructure_error(monkeypatch):
    store = S3ObjectStore("lanes")
    monkeypatch.setattr(store, "_client", lambda: _FakeCM(ConnectionError("refused")))
    with pytest.raises(ObjectStoreError):
        await store.put("k", b"data")


@pytest.mark.asyncio
async def test_non_infra_error_is_not_reclassified(monkeypatch):
    store = S3ObjectStore("lanes")
    monkeypatch.setattr(store, "_client", lambda: _FakeCM(ValueError("bug")))
    with pytest.raises(ValueError):
        await store.put("k", b"data")


@pytest.mark.asyncio
async def test_aclose_is_safe():
    store = S3ObjectStore("lanes")
    await store.aclose()  # no session yet → no-op, must not raise
