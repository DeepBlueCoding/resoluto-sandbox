"""S3-backed Conduit (minio or any S3-compatible store)."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager

from resoluto.sandbox.contracts import Conduit, ConduitError, ConduitKeyMissing, ObjectInfo

# Permanent authorization/authentication denials — NOT transient transport failures. These must
# propagate as the raw ClientError (e.g. a scoped-credential cross-prefix write being denied), not
# be masked as a retryable "object store I/O failed".
_AUTH_ERROR_CODES = frozenset(
    {
        "AccessDenied",
        "AccessDeniedException",
        "InvalidAccessKeyId",
        "SignatureDoesNotMatch",
        "ExpiredToken",
        "ExpiredTokenException",
        "InvalidToken",
        "UnauthorizedAccess",
    }
)


def _is_infra_error(exc: BaseException) -> bool:
    """True for transport/storage failures rather than ordinary application or authorization errors."""
    try:
        from botocore.exceptions import BotoCoreError, ClientError

        if isinstance(exc, ClientError):
            code = exc.response.get("Error", {}).get("Code", "")
            return code not in _AUTH_ERROR_CODES
        if isinstance(exc, BotoCoreError):
            return True
    except Exception:
        pass
    return isinstance(exc, (ConnectionError, TimeoutError, OSError))


def _build_scoped_policy(bucket: str, prefix: str) -> str:
    """Build an IAM policy JSON scoped to <bucket>/<prefix>/* with prefix-scoped ListBucket."""
    return json.dumps(
        {
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
        }
    )


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
    session_name = f"resoluto-sandbox-{prefix.replace('/', '-')}"[:64]
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


class S3Conduit(Conduit):
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
        cfg = Config(
            retries={"max_attempts": 10, "mode": "standard"},
            connect_timeout=10,
            read_timeout=30,
        )
        kwargs = {k: v for k, v in self._client_kwargs.items() if v is not None}
        return self._session.client("s3", config=cfg, **kwargs)

    @asynccontextmanager
    async def _io(self):
        """Open a client and translate transport failures into a typed ConduitError."""
        try:
            async with self._client() as c:
                yield c
        except Exception as exc:
            if _is_infra_error(exc):
                raise ConduitError(
                    f"object store I/O failed (bucket={self._bucket}): {exc}"
                ) from exc
            raise

    async def aclose(self) -> None:
        """Drop the cached session so a stale one isn't reused after teardown."""
        self._session = None

    async def put(self, key: str, data: bytes) -> None:
        async with self._io() as c:
            await c.put_object(Bucket=self._bucket, Key=key, Body=data)

    async def get(self, key: str) -> bytes:
        try:
            async with self._io() as c:
                resp = await c.get_object(Bucket=self._bucket, Key=key)
                async with resp["Body"] as body:
                    return await body.read()
        except ConduitError as exc:
            cause = exc.__cause__
            code = getattr(cause, "response", {}).get("Error", {}).get("Code", "")
            if code in ("NoSuchKey", "404"):
                raise ConduitKeyMissing(f"no such key: {key}") from cause
            raise

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
        src, dst = src_prefix.rstrip("/"), dst_prefix.rstrip("/")
        objs = await self.list_prefix(src)
        async with self._io() as c:
            for o in objs:
                rel = o.key[len(src) :].lstrip("/")
                await c.copy_object(
                    Bucket=self._bucket,
                    Key=f"{dst}/{rel}",
                    CopySource={"Bucket": self._bucket, "Key": o.key},
                )
        return len(objs)

    async def delete_prefix(self, prefix: str) -> int:
        objs = await self.list_prefix(prefix.rstrip("/") + "/")
        n = 0
        async with self._io() as c:
            for i in range(0, len(objs), 1000):
                batch = [{"Key": o.key} for o in objs[i : i + 1000]]
                await c.delete_objects(Bucket=self._bucket, Delete={"Objects": batch})
                n += len(batch)
        return n

    async def ensure_bucket(self) -> None:
        """Create the bucket if absent."""
        from botocore.exceptions import ClientError

        async with self._client() as c:
            try:
                await c.head_bucket(Bucket=self._bucket)
            except ClientError:
                await c.create_bucket(Bucket=self._bucket)
