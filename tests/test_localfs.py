import pytest

from resoluto_sandbox import LocalFsObjectStore


@pytest.mark.asyncio
async def test_put_get_range(tmp_path):
    s = LocalFsObjectStore(tmp_path)
    await s.put("run/r/a.txt", b"0123456789")
    assert await s.get("run/r/a.txt") == b"0123456789"
    assert await s.get("run/r/a.txt", 3) == b"3456789"
    assert await s.get("run/r/a.txt", 2, 5) == b"234"


@pytest.mark.asyncio
async def test_list_prefix_sorted_and_sized(tmp_path):
    s = LocalFsObjectStore(tmp_path)
    await s.put("run/r/events-000002.jsonl", b"bb")
    await s.put("run/r/events-000001.jsonl", b"a")
    infos = await s.list_prefix("run/r")
    assert [i.key for i in infos] == ["run/r/events-000001.jsonl", "run/r/events-000002.jsonl"]
    assert [i.size for i in infos] == [1, 2]


@pytest.mark.asyncio
async def test_partial_writes_not_listed(tmp_path):
    s = LocalFsObjectStore(tmp_path)
    # a leftover partial must never be visible to the reader (atomicity invariant)
    (tmp_path / "run").mkdir()
    (tmp_path / "run" / "events-000001.jsonl.tmp-partial").write_bytes(b"half")
    assert await s.list_prefix("run") == []


@pytest.mark.asyncio
async def test_key_traversal_rejected(tmp_path):
    s = LocalFsObjectStore(tmp_path)
    with pytest.raises(ValueError, match="escapes store root"):
        await s.put("../escape.txt", b"x")
