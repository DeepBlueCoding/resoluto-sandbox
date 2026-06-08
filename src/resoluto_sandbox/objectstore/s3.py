"""S3ObjectStore — the portable middle (minio locally, S3/any-S3-API in cloud).

The bucket is the store root; keys are full object keys (parity with LocalFs).
A prefix-scoped, write-only, expiring credential (§12.3) is supplied to the
sandbox; the orchestrator-side reader uses fuller creds. Lazy aioboto3 import
(behind the [s3] extra)."""
from __future__ import annotations

from resoluto_sandbox.contracts import ObjectInfo, ObjectStore


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

        if self._session is None:
            self._session = aioboto3.Session()
        # aioboto3 clients are async context managers — one per call (robust;
        # avoids a long-lived connection, consistent with the no-stream principle).
        return self._session.client("s3", **{k: v for k, v in self._client_kwargs.items() if v is not None})

    async def put(self, key: str, data: bytes) -> None:
        async with self._client() as c:
            await c.put_object(Bucket=self._bucket, Key=key, Body=data)

    async def get(self, key: str) -> bytes:
        async with self._client() as c:
            resp = await c.get_object(Bucket=self._bucket, Key=key)
            async with resp["Body"] as body:
                return await body.read()

    async def list_prefix(self, prefix: str) -> list[ObjectInfo]:
        out: list[ObjectInfo] = []
        async with self._client() as c:
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

    async def ensure_bucket(self) -> None:
        """Dev convenience — create the bucket if absent (minio/local)."""
        from botocore.exceptions import ClientError

        async with self._client() as c:
            try:
                await c.head_bucket(Bucket=self._bucket)
            except ClientError:
                await c.create_bucket(Bucket=self._bucket)
