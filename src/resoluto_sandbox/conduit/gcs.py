"""GcsConduit — the cloud backend (GKE + GCS via Workload Identity).

Same contract as LocalConduit/S3Conduit. Uses gcloud-aio-storage (async); lazy import behind
the [gcs] extra. NOTE: not locally integration-tested (no GCP creds in the spike
env) — validated by contract parity with S3 (which IS minio-tested); the
conformance suite should run against a real bucket before relying on it in
production (a conformance suite should run against a real bucket before relying on it)."""
from __future__ import annotations

from resoluto_sandbox.contracts import Conduit, ObjectInfo


class GcsConduit(Conduit):
    def __init__(self, bucket: str, *, service_file: str | None = None) -> None:
        self._bucket = bucket
        self._service_file = service_file  # None → Workload Identity / ADC
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
        # Server-side copy (no host round-trip). GCS is not integration-tested in
        # this env (see module docstring); if the kwarg name drifts across
        # gcloud-aio-storage versions the ABC's get/put default still copies.
        src, dst = src_prefix.rstrip("/"), dst_prefix.rstrip("/")
        client = self._client()
        objs = await self.list_prefix(src)
        for o in objs:
            rel = o.key[len(src):].lstrip("/")
            await client.copy(self._bucket, o.key, self._bucket, new_name=f"{dst}/{rel}")
        return len(objs)

    async def close(self) -> None:
        if self._storage is not None:
            await self._storage.close()
