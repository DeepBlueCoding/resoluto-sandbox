"""S3ObjectStore integration against a real minio (proves the store-mediated
telemetry works over a real object store, not just localfs).

Run:  uv run pytest -m integration   (needs minio on :9100, see test setup)
"""
import uuid

import pytest

from resoluto_sandbox import ChunkReader, ChunkShipper, SpanEvent
from resoluto_sandbox.objectstore.s3 import S3ObjectStore

ENDPOINT = "http://localhost:9100"
CREDS = dict(aws_access_key_id="minioadmin", aws_secret_access_key="minioadmin", region_name="us-east-1")


async def _store():
    s = S3ObjectStore("resoluto-spike", endpoint_url=ENDPOINT, **CREDS)
    await s.ensure_bucket()
    return s


@pytest.mark.integration
@pytest.mark.asyncio
async def test_s3_put_get_list():
    s = await _store()
    pfx = f"t/{uuid.uuid4().hex}"
    await s.put(f"{pfx}/a.txt", b"0123456789")
    assert await s.get(f"{pfx}/a.txt") == b"0123456789"
    await s.put(f"{pfx}/b.txt", b"xy")
    infos = await s.list_prefix(pfx)
    assert {i.key for i in infos} == {f"{pfx}/a.txt", f"{pfx}/b.txt"}
    assert {i.key: i.size for i in infos}[f"{pfx}/b.txt"] == 2


@pytest.mark.integration
@pytest.mark.asyncio
async def test_telemetry_over_s3():
    """The full store-mediated comms path over a real object store."""
    s = await _store()
    prefix = f"run/{uuid.uuid4().hex}/nodes/compete"
    ship = ChunkShipper(s, prefix, flush_bytes=10_000)
    reader = ChunkReader(s, prefix)

    await ship.emit(SpanEvent(run_id="r", span_id="s1", kind="node", name="start", event="open", ts=0))
    await ship.flush()
    assert [e.name for e in await reader.poll()] == ["start"]

    await ship.emit(SpanEvent(run_id="r", span_id="s2", kind="node", name="compete", event="close", ts=1, status="success"))
    await ship.close()
    out = await reader.poll()
    assert [e.name for e in out] == ["compete"]
    assert out[0].status == "success"
    assert reader.finished is True
