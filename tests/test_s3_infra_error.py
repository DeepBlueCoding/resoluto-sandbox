"""S3Conduit translates transport failures (e.g. minio storage-full) into a
typed ConduitError so the host can fail the run fast with the real cause —
while ordinary application errors pass through unreclassified."""
import pytest

from resoluto.sandbox.contracts import ConduitError
from resoluto.sandbox.conduit.s3 import S3Conduit


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


def _client_error():
    from botocore.exceptions import ClientError
    return ClientError(
        {"Error": {"Code": "XMinioStorageFull", "Message": "min free drive threshold"}},
        "PutObject",
    )


@pytest.mark.parametrize(
    "make_exc, expected, must_contain",
    [
        (_client_error, ConduitError, ["object store I/O failed", "XMinioStorageFull"]),
        (lambda: ConnectionError("refused"), ConduitError, []),
        (lambda: ValueError("bug"), ValueError, []),  # application error is NOT reclassified
    ],
)
async def test_put_reclassifies_only_transport_failures(monkeypatch, make_exc, expected, must_contain):
    store = S3Conduit("sandboxes")
    monkeypatch.setattr(store, "_client", lambda: _FakeCM(make_exc()))
    with pytest.raises(expected) as ei:
        await store.put("k", b"data")
    for fragment in must_contain:
        assert fragment in str(ei.value)


async def test_aclose_is_safe():
    store = S3Conduit("sandboxes")
    await store.aclose()  # no session yet → no-op, must not raise
