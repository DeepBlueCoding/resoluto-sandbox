import pytest

from resoluto_sandbox import (
    ChunkReader,
    ChunkShipper,
    LocalFsObjectStore,
    SpanEvent,
)


def _ev(name, ts=0.0):
    return SpanEvent(run_id="r1", span_id=name, kind="node", name=name, event="open", ts=ts)


@pytest.mark.asyncio
async def test_ship_then_read_roundtrip(tmp_path):
    store = LocalFsObjectStore(tmp_path)
    prefix = "run/r1/nodes/compete"
    ship = ChunkShipper(store, prefix, flush_bytes=10_000)
    reader = ChunkReader(store, prefix)

    await ship.emit(_ev("start"))
    await ship.emit(_ev("plan"))
    await ship.flush()  # chunk 1
    got = await reader.poll()
    assert [e.name for e in got] == ["start", "plan"]

    await ship.emit(_ev("compete"))
    await ship.close()  # flushes chunk 2 + manifest
    got2 = await reader.poll()
    assert [e.name for e in got2] == ["compete"]
    assert reader.finished is True


@pytest.mark.asyncio
async def test_reconnect_resumes_at_index(tmp_path):
    store = LocalFsObjectStore(tmp_path)
    prefix = "run/r/nodes/n"
    ship = ChunkShipper(store, prefix, flush_bytes=10_000)
    await ship.emit(_ev("a")); await ship.flush()
    await ship.emit(_ev("b")); await ship.flush()

    # a fresh reader (simulating orchestrator restart) replays from the store
    r1 = ChunkReader(store, prefix)
    assert [e.name for e in await r1.poll()] == ["a", "b"]
    # a second reader sees the same — durability through reader death
    r2 = ChunkReader(store, prefix)
    assert [e.name for e in await r2.poll()] == ["a", "b"]


@pytest.mark.asyncio
async def test_liveness_is_chunk_arrival(tmp_path):
    clock = {"t": 0.0}
    store = LocalFsObjectStore(tmp_path)
    prefix = "run/r/nodes/n"
    ship = ChunkShipper(store, prefix, flush_bytes=10_000, clock=lambda: clock["t"])
    reader = ChunkReader(store, prefix, dead_after_s=100.0, clock=lambda: clock["t"])

    await ship.emit(_ev("x")); await ship.flush()
    await reader.poll()
    assert reader.is_dead() is False
    clock["t"] = 50.0
    await reader.poll()
    assert reader.is_dead() is False  # within window
    clock["t"] = 200.0
    await reader.poll()
    assert reader.is_dead() is True  # no new chunk → substrate dead


@pytest.mark.asyncio
async def test_finished_run_is_never_dead(tmp_path):
    clock = {"t": 0.0}
    store = LocalFsObjectStore(tmp_path)
    prefix = "run/r/nodes/n"
    ship = ChunkShipper(store, prefix, flush_bytes=10_000, clock=lambda: clock["t"])
    reader = ChunkReader(store, prefix, dead_after_s=10.0, clock=lambda: clock["t"])
    await ship.emit(_ev("only")); await ship.close()
    await reader.poll()
    assert reader.finished is True
    clock["t"] = 9999.0
    assert reader.is_dead() is False  # cleanly finished, not dead


@pytest.mark.asyncio
async def test_terminal_gap_surfaces_as_dead(tmp_path):
    clock = {"t": 0.0}
    store = LocalFsObjectStore(tmp_path)
    prefix = "run/r/nodes/n"
    reader = ChunkReader(store, prefix, dead_after_s=50.0, clock=lambda: clock["t"])
    # write chunk 2 but NOT chunk 1, plus a manifest claiming 2 chunks → a gap
    await store.put(f"{prefix}/events-000002.jsonl", b'{"run_id":"r","span_id":"b","kind":"node","event":"open","ts":0}\n')
    import json
    await store.put(f"{prefix}/_manifest.json", json.dumps({"total_chunks": 2}).encode())
    await reader.poll()           # chunk 1 missing → contiguous progress stalls at 0
    assert reader.is_dead() is False  # still within the window
    clock["t"] = 100.0
    await reader.poll()
    assert reader.is_dead() is True   # past the window, never finished → dead (single signal)
