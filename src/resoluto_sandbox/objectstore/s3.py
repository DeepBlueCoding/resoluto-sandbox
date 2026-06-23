"""S3ObjectStore — the portable middle (minio locally, S3/any-S3-API in cloud).

The bucket is the store root; keys are full object keys (parity with LocalFs).
A prefix-scoped, write-only, expiring credential (§12.3) is supplied to the
sandbox; the orchestrator-side reader uses fuller creds. Lazy aioboto3 import
(behind the [s3] extra)."""
from __future__ import annotations

import json
from contextlib import asynccontextmanager

from resoluto_sandbox.contracts import ObjectInfo, ObjectStore, ObjectStoreError


def _is_infra_error(exc: BaseException) -> bool:
    """True for substrate/transport failures (S3 ClientError incl. storage-full,
    botocore, connection/OS errors) — as opposed to ordinary application errors."""
    try:
        from botocore.exceptions import BotoCoreError, ClientError
        if isinstance(exc, (BotoCoreError, ClientError)):
            return True
    except Exception:
        pass
    return isinstance(exc, (ConnectionError, TimeoutError, OSError))


def _build_scoped_policy(bucket: str, prefix: str) -> str:
    """Build an IAM policy JSON scoped to <bucket>/<prefix>/*: object read/write PLUS
    prefix-scoped ListBucket. ListBucket is required because the sandbox's stage_inputs
    lists `<prefix>/inbox/` (ListObjectsV2) before fetching — without it the pod gets
    AccessDenied and crashes before shipping any telemetry (a silent lane death)."""
    return json.dumps({
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Action": ["s3:PutObject", "s3:GetObject"],
                "Resource": f"arn:aws:s3:::{bucket}/{prefix}/*",
            },
            {
                "Effect": "Allow",
                "Action": ["s3:ListBucket"],
                "Resource": f"arn:aws:s3:::{bucket}",
                "Condition": {"StringLike": {"s3:prefix": [f"{prefix}/*"]}},
            },
        ],
    })


async def mint_scoped_credential(
    bucket: str,
    prefix: str,
    endpoint_url: str | None,
    region: str,
    access_key_id: str,
    secret_access_key: str,
    *,
    ttl_seconds: int = 3600,
    sts_role_arn: str,
) -> dict:
    """Mint a prefix-scoped, expiring STS credential for bucket/prefix.

    Returns dict with access_key_id, secret_access_key, session_token, bucket,
    endpoint_url, region.
    """
    import aioboto3

    session = aioboto3.Session()
    session_name = f"resoluto-lane-{prefix.replace('/', '-')}"[:64]
    async with session.client(
        "sts",
        endpoint_url=endpoint_url,
        region_name=region,
        aws_access_key_id=access_key_id,
        aws_secret_access_key=secret_access_key,
    ) as sts:
        resp = await sts.assume_role(
            RoleArn=sts_role_arn,
            RoleSessionName=session_name,
            Policy=_build_scoped_policy(bucket, prefix),
            DurationSeconds=ttl_seconds,
        )
    creds = resp["Credentials"]
    return {
        "access_key_id": creds["AccessKeyId"],
        "secret_access_key": creds["SecretAccessKey"],
        "session_token": creds["SessionToken"],
        "bucket": bucket,
        "endpoint_url": endpoint_url,
        "region": region,
    }


class S3ObjectStore(ObjectStore):
    def __init__(
        self,
        bucket: str,
        *,
        endpoint_url: str | None = None,
        region_name: str | None = None,
        aws_access_key_id: str | None = None,
        aws_secret_access_key: str | None = None,
        aws_session_token: str | None = None,
    ) -> None:
        self._bucket = bucket
        self._client_kwargs = {
            "endpoint_url": endpoint_url,
            "region_name": region_name,
            "aws_access_key_id": aws_access_key_id,
            "aws_secret_access_key": aws_secret_access_key,
            "aws_session_token": aws_session_token,
        }
        self._session = None

    def _client(self):
        import aioboto3
        from botocore.config import Config

        if self._session is None:
            self._session = aioboto3.Session()
        # A 20-min lane is tailed by polling this store every few seconds; a
        # transient connection blip under load must be absorbed, not abort the
        # drive (§11.2 liveness is time-bounded by the death-window, not by one
        # failed read). Standard mode retries the connection-error family with
        # backoff; bounded timeouts fail a hung socket fast so the retry fires.
        cfg = Config(
            retries={"max_attempts": 10, "mode": "standard"},
            connect_timeout=10,
            read_timeout=30,
        )
        # aioboto3 clients are async context managers — one per call (robust;
        # avoids a long-lived connection, consistent with the no-stream principle).
        kwargs = {k: v for k, v in self._client_kwargs.items() if v is not None}
        return self._session.client("s3", config=cfg, **kwargs)

    @asynccontextmanager
    async def _io(self):
        """Open a client and translate transport failures into a typed
        ObjectStoreError (substrate-native), so the worker can fail the run fast
        with the real cause (e.g. minio storage-full) instead of leaking a raw
        botocore traceback that gets misclassified as an agent failure."""
        try:
            async with self._client() as c:
                yield c
        except Exception as exc:
            if _is_infra_error(exc):
                raise ObjectStoreError(f"object store I/O failed (bucket={self._bucket}): {exc}") from exc
            raise

    async def aclose(self) -> None:
        """Drop the cached session so a stale one isn't reused after teardown.
        (Clients are per-call async context managers, closed on exit.)"""
        self._session = None

    async def put(self, key: str, data: bytes) -> None:
        async with self._io() as c:
            await c.put_object(Bucket=self._bucket, Key=key, Body=data)

    async def get(self, key: str) -> bytes:
        async with self._io() as c:
            resp = await c.get_object(Bucket=self._bucket, Key=key)
            async with resp["Body"] as body:
                return await body.read()

    async def list_prefix(self, prefix: str) -> list[ObjectInfo]:
        out: list[ObjectInfo] = []
        async with self._io() as c:
            token: str | None = None
            while True:
                kwargs = {"Bucket": self._bucket, "Prefix": prefix}
                if token:
                    kwargs["ContinuationToken"] = token
                resp = await c.list_objects_v2(**kwargs)
                for obj in resp.get("Contents", []):
                    out.append(ObjectInfo(key=obj["Key"], size=obj["Size"]))
                if not resp.get("IsTruncated"):
                    break
                token = resp.get("NextContinuationToken")
        out.sort(key=lambda i: i.key)
        return out

    async def copy_prefix(self, src_prefix: str, dst_prefix: str) -> int:
        # Server-side CopyObject — the bytes never round-trip through the host
        # (the ~184MB/lane worktree stays in the store). Single-part copy caps at
        # 5GB/object; lane payloads are well under, so no multipart needed.
        src, dst = src_prefix.rstrip("/"), dst_prefix.rstrip("/")
        objs = await self.list_prefix(src)
        async with self._io() as c:
            for o in objs:
                rel = o.key[len(src):].lstrip("/")
                await c.copy_object(
                    Bucket=self._bucket,
                    Key=f"{dst}/{rel}",
                    CopySource={"Bucket": self._bucket, "Key": o.key},
                )
        return len(objs)

    async def ensure_bucket(self) -> None:
        """Dev convenience — create the bucket if absent (minio/local)."""
        from botocore.exceptions import ClientError

        async with self._client() as c:
            try:
                await c.head_bucket(Bucket=self._bucket)
            except ClientError:
                await c.create_bucket(Bucket=self._bucket)
