"""Hermetic unit tests for GcsConduit — a selectable production backend (RESOLUTO_STORE_KIND=gcs)
that otherwise has no coverage. The gcloud Storage client is stubbed at the _client() seam, so no
gcloud dep and no network. Pins list_prefix pagination (nextPageToken) and copy_prefix's
suffix-relativization — the off-by-one logic that breaks silently."""
import pytest

from resoluto_sandbox.conduit.gcs import GcsConduit


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
async def test_aclose_is_a_noop_when_never_used():
    c = GcsConduit("my-bucket")  # never touched _client(), so self._storage is still None
    await c.aclose()  # must not raise


def _conduit(storage) -> GcsConduit:
    c = GcsConduit("my-bucket")
    c._storage = storage              # bypass lazy gcloud import
    c._client = lambda: storage       # _client() returns the fake
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

    assert [o.key for o in objs] == ["p/a", "p/b", "p/c"]   # sorted across both pages
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
