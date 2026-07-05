"""S3Conduit integration against a real minio (proves the store-mediated
telemetry works over a real object store, not just localfs).

Run:  uv run pytest -m integration   (needs minio on :9100, see test setup)

STS / prefix-isolation test (test_cross_prefix_isolation) additionally requires
minio started with STS AssumeRole support, e.g.:
    minio server /data --console-address :9101

And a role ARN reachable from the minio STS endpoint. For local dev, you can
set the role ARN to the minio wildcard: "arn:aws:iam::123456789012:role/test".
Set MINIO_STS_ROLE_ARN env var to override the default used in the test.
"""
import os
import uuid

import pytest

from resoluto.sandbox import ChunkReader, ChunkShipper, SpanEvent
from resoluto.sandbox.conduit.s3 import S3Conduit, mint_scoped_credential

ENDPOINT = "http://localhost:9100"
CREDS = dict(aws_access_key_id="minioadmin", aws_secret_access_key="minioadmin", region_name="us-east-1")


async def _store():
    s = S3Conduit("resoluto-spike", endpoint_url=ENDPOINT, **CREDS)
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
async def test_cross_prefix_isolation():
    """Mint two scoped tokens for different prefixes; each can write to its own
    prefix but is denied on the other's prefix (cross-run isolation, §12.3).

    Requires minio with STS AssumeRole enabled and MINIO_STS_ROLE_ARN set.
    """
    from botocore.exceptions import ClientError

    role_arn = os.environ.get("MINIO_STS_ROLE_ARN", "arn:aws:iam::123456789012:role/resoluto-test")
    bucket = "resoluto-spike"
    run_a = f"run/{uuid.uuid4().hex}/nodes/a"
    run_b = f"run/{uuid.uuid4().hex}/nodes/b"

    # Ensure bucket exists using admin creds
    admin_store = S3Conduit(bucket, endpoint_url=ENDPOINT, **CREDS)
    await admin_store.ensure_bucket()

    # Mint two scoped tokens for different prefixes
    tok_a = await mint_scoped_credential(
        bucket, run_a, ENDPOINT, "us-east-1",
        CREDS["aws_access_key_id"], CREDS["aws_secret_access_key"],
        sts_role_arn=role_arn,
    )
    tok_b = await mint_scoped_credential(
        bucket, run_b, ENDPOINT, "us-east-1",
        CREDS["aws_access_key_id"], CREDS["aws_secret_access_key"],
        sts_role_arn=role_arn,
    )

    store_a = S3Conduit(
        tok_a["bucket"], endpoint_url=tok_a["endpoint_url"],
        region_name=tok_a["region"],
        aws_access_key_id=tok_a["access_key_id"],
        aws_secret_access_key=tok_a["secret_access_key"],
        aws_session_token=tok_a["session_token"],
    )
    store_b = S3Conduit(
        tok_b["bucket"], endpoint_url=tok_b["endpoint_url"],
        region_name=tok_b["region"],
        aws_access_key_id=tok_b["access_key_id"],
        aws_secret_access_key=tok_b["secret_access_key"],
        aws_session_token=tok_b["session_token"],
    )

    # Each store can write to its own prefix
    await store_a.put(f"{run_a}/result.json", b'{"status":"ok"}')
    await store_b.put(f"{run_b}/result.json", b'{"status":"ok"}')

    # Cross-prefix write must be denied
    with pytest.raises(ClientError) as exc_a:
        await store_a.put(f"{run_b}/evil.json", b"injected")
    assert exc_a.value.response["Error"]["Code"] in ("AccessDenied", "403")

    with pytest.raises(ClientError) as exc_b:
        await store_b.put(f"{run_a}/evil.json", b"injected")
    assert exc_b.value.response["Error"]["Code"] in ("AccessDenied", "403")


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
