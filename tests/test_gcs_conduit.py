"""Hermetic unit tests for GcsConduit — a selectable production backend (RESOLUTO_STORE_KIND=gcs)
that otherwise has no coverage. The gcloud Storage client is stubbed at the _client() seam, so no
gcloud dep and no network. Pins list_prefix pagination (nextPageToken) and copy_prefix's
suffix-relativization — the off-by-one logic that breaks silently."""

import asyncio

import pytest

from resoluto.sandbox.conduit.gcs import GcsConduit
from resoluto.sandbox.contracts import ConduitError


class _HttpError(Exception):
    """Stand-in for a gcloud-aio / aiohttp error carrying an HTTP status."""

    def __init__(self, status: int) -> None:
        super().__init__(f"HTTP {status}")
        self.status = status


async def _nosleep(_delay) -> None:
    return None


class _FakeStorage:
    """Stand-in for gcloud.aio.storage.Storage: pages keyed by pageToken (None = first page)."""

    def __init__(self, pages):
        self._pages = pages
        self.copies: list[tuple[str, str]] = []
        self.closed = False

    async def list_objects(self, bucket, params=None):
        return self._pages[(params or {}).get("pageToken")]

    async def copy(self, bucket, name, dst_bucket, new_name=None):
        self.copies.append((name, new_name))

    async def close(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_aclose_closes_the_cached_storage_client():
    # aclose() (not close()) is the one name every Conduit uses (see contracts.py:Conduit.aclose).
    storage = _FakeStorage({})
    c = _conduit(storage)
    await c.aclose()
    assert storage.closed is True


@pytest.mark.asyncio
async def test_aclose_resets_storage_so_client_lazily_recreates():
    # A reused Conduit calls aclose() after every run() (SubstrateBackend._run_async's finally).
    # If _storage weren't reset to None, the NEXT _client() call would hand back the closed client.
    c = GcsConduit("my-bucket")
    c._storage = _FakeStorage({})  # simulate _client() having been called once already
    await c.aclose()
    assert c._storage is None


@pytest.mark.asyncio
async def test_aclose_is_a_noop_when_never_used():
    c = GcsConduit("my-bucket")  # never touched _client(), so self._storage is still None
    await c.aclose()  # must not raise


def _conduit(storage) -> GcsConduit:
    c = GcsConduit("my-bucket")
    c._storage = storage  # bypass lazy gcloud import
    c._client = lambda: storage  # _client() returns the fake
    return c


async def test_list_prefix_paginates_and_returns_sorted_objectinfo():
    pages = {
        None: {
            "items": [{"name": "p/b", "size": "2"}, {"name": "p/a", "size": "5"}],
            "nextPageToken": "t1",
        },
        "t1": {"items": [{"name": "p/c", "size": "3"}]},  # no nextPageToken → stop
    }
    conduit = _conduit(_FakeStorage(pages))

    objs = await conduit.list_prefix("p")

    assert [o.key for o in objs] == ["p/a", "p/b", "p/c"]  # sorted across both pages
    assert {o.key: o.size for o in objs} == {"p/a": 5, "p/b": 2, "p/c": 3}  # sizes coerced to int


async def test_copy_prefix_mirrors_suffixes_and_counts():
    pages = {
        None: {"items": [{"name": "run/a/x", "size": "1"}, {"name": "run/a/d/y", "size": "1"}]},
    }
    storage = _FakeStorage(pages)
    conduit = _conduit(storage)

    n = await conduit.copy_prefix("run/a", "run/b")

    assert n == 2
    # suffix relative to src is mirrored under dst (no leading slash, nested path preserved);
    # objects are visited in list_prefix's sorted order, so "d/y" precedes "x".
    assert storage.copies == [("run/a/d/y", "run/b/d/y"), ("run/a/x", "run/b/x")]


async def test_copy_prefix_handles_trailing_slash_on_prefixes():
    pages = {None: {"items": [{"name": "src/only", "size": "0"}]}}
    storage = _FakeStorage(pages)
    conduit = _conduit(storage)

    n = await conduit.copy_prefix("src/", "dst/")

    assert n == 1
    assert storage.copies == [("src/only", "dst/only")]


async def test_io_retries_transient_then_succeeds(monkeypatch):
    # A 503 is transient — retry (with backoff) until it clears, mirroring S3's standard-mode retry.
    monkeypatch.setattr(asyncio, "sleep", _nosleep)
    calls = {"n": 0}

    async def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise _HttpError(503)
        return "ok"

    result = await GcsConduit("b")._io("get", flaky)

    assert result == "ok"
    assert calls["n"] == 3


async def test_io_auth_error_propagates_raw(monkeypatch):
    # 403 is a permanent denial (e.g. a cross-prefix write) — it must NOT be masked as ConduitError.
    monkeypatch.setattr(asyncio, "sleep", _nosleep)

    async def denied():
        raise _HttpError(403)

    with pytest.raises(_HttpError):
        await GcsConduit("b")._io("put", denied)


async def test_io_terminal_transport_error_becomes_conduit_error(monkeypatch):
    # A transport error that never clears is surfaced as a typed ConduitError after retries exhaust.
    monkeypatch.setattr(asyncio, "sleep", _nosleep)

    async def broken():
        raise ConnectionError("connection reset")

    with pytest.raises(ConduitError, match="object store I/O failed"):
        await GcsConduit("b")._io("get", broken)
