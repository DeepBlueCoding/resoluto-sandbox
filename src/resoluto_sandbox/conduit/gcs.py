"""GCS-backed Conduit via gcloud-aio-storage (lazy import behind the [gcs] extra)."""
from __future__ import annotations

from resoluto_sandbox.contracts import Conduit, ObjectInfo


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

    async def put(self, key: str, data: bytes) -> None:
        await self._client().upload(self._bucket, key, data)

    async def get(self, key: str) -> bytes:
        return await self._client().download(self._bucket, key)

    async def list_prefix(self, prefix: str) -> list[ObjectInfo]:
        client = self._client()
        out: list[ObjectInfo] = []
        params = {"prefix": prefix}
        while True:
            resp = await client.list_objects(self._bucket, params=params)
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
            rel = o.key[len(src):].lstrip("/")
            await client.copy(self._bucket, o.key, self._bucket, new_name=f"{dst}/{rel}")
        return len(objs)

    async def aclose(self) -> None:
        if self._storage is not None:
            await self._storage.close()
