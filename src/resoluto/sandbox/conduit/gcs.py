"""GCS-backed Conduit via gcloud-aio-storage (lazy import behind the [gcs] extra).

Host-side, SINGLE-TENANT store (e.g. the Orchestrator's own rendezvous). Unlike S3Conduit it has
NO per-prefix scoped-credential minting (S3's STS AssumeRole path): a GcsConduit authenticates with
the WHOLE service account, so it must NOT back a multi-tenant lane store where each lane needs
prefix isolation. Transport/throttling failures are retried and surfaced as ConduitError, mirroring
S3Conduit's botocore standard-mode retries; permanent auth denials (401/403) propagate raw.
"""

from __future__ import annotations

import asyncio

from resoluto.sandbox.contracts import Conduit, ConduitError, ObjectInfo

_MAX_ATTEMPTS = 10
_AUTH_STATUSES = frozenset({401, 403})


def _status_of(exc: BaseException) -> int | None:
    """HTTP status carried by a gcloud-aio / aiohttp error, if any."""
    return getattr(exc, "status", None)


def _is_auth_error(exc: BaseException) -> bool:
    return _status_of(exc) in _AUTH_STATUSES


def _is_transient(exc: BaseException) -> bool:
    """True for transport / 5xx / throttling failures worth retrying — never an auth denial."""
    status = _status_of(exc)
    if status is not None:
        return status == 429 or status >= 500
    return isinstance(exc, (ConnectionError, TimeoutError, OSError))


class GcsConduit(Conduit):
    def __init__(self, bucket: str, *, service_file: str | None = None) -> None:
        self._bucket = bucket
        self._service_file = service_file
        self._storage = None

    def _client(self):
        from gcloud.aio.storage import Storage

        if self._storage is None:
            self._storage = Storage(service_file=self._service_file)
        return self._storage

    async def _io(self, label, op):
        """Run one store op with bounded transient-fault retry (exponential backoff). Terminal
        transport failures become a typed ConduitError; auth denials propagate raw (never masked)."""
        attempt = 0
        while True:
            try:
                return await op()
            except Exception as exc:
                if _is_transient(exc) and attempt < _MAX_ATTEMPTS - 1:
                    attempt += 1
                    await asyncio.sleep(min(0.1 * 2**attempt, 5.0))
                    continue
                if _is_auth_error(exc):
                    raise
                raise ConduitError(
                    f"object store I/O failed (bucket={self._bucket}, op={label}): {exc}"
                ) from exc

    async def put(self, key: str, data: bytes) -> None:
        await self._io("put", lambda: self._client().upload(self._bucket, key, data))

    async def get(self, key: str) -> bytes:
        return await self._io("get", lambda: self._client().download(self._bucket, key))

    async def list_prefix(self, prefix: str) -> list[ObjectInfo]:
        client = self._client()
        out: list[ObjectInfo] = []
        params = {"prefix": prefix}
        while True:
            resp = await self._io(
                "list", lambda p=params: client.list_objects(self._bucket, params=p)
            )
            for item in resp.get("items", []):
                out.append(ObjectInfo(key=item["name"], size=int(item.get("size", 0))))
            token = resp.get("nextPageToken")
            if not token:
                break
            params = {"prefix": prefix, "pageToken": token}
        out.sort(key=lambda i: i.key)
        return out

    async def copy_prefix(self, src_prefix: str, dst_prefix: str) -> int:
        src, dst = src_prefix.rstrip("/"), dst_prefix.rstrip("/")
        client = self._client()
        objs = await self.list_prefix(src)
        for o in objs:
            rel = o.key[len(src) :].lstrip("/")
            await self._io(
                "copy",
                lambda o=o, rel=rel: client.copy(
                    self._bucket, o.key, self._bucket, new_name=f"{dst}/{rel}"
                ),
            )
        return len(objs)

    async def aclose(self) -> None:
        if self._storage is not None:
            await self._storage.close()
            self._storage = None  # so _client() lazily recreates on next use, not a closed session
