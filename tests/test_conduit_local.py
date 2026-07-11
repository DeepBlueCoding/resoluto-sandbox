"""LocalConduit delete_prefix — the retention/GC mechanism (policy lives with the caller)."""

from resoluto.sandbox.conduit import LocalConduit


async def test_delete_prefix_removes_the_subtree_and_counts(tmp_path):
    store = LocalConduit(tmp_path)
    await store.put("run/r1/a.txt", b"x")
    await store.put("run/r1/nested/b.txt", b"y")
    await store.put("run/r2/keep.txt", b"z")

    assert await store.delete_prefix("run/r1") == 2
    assert [o.key for o in await store.list_prefix("run")] == ["run/r2/keep.txt"]
    assert await store.delete_prefix("run/does-not-exist") == 0


async def test_get_missing_key_raises_the_typed_state_signal(tmp_path):
    import pytest

    from resoluto.sandbox.contracts import ConduitError, ConduitKeyMissing

    store = LocalConduit(tmp_path)
    with pytest.raises(ConduitKeyMissing):
        await store.get("run/nothing/here")
    assert issubclass(ConduitKeyMissing, ConduitError)  # outage handlers still catch it
