"""GcsObjectStore — the cloud backend (GKE + GCS via Workload Identity).

Same contract as LocalFs/S3. Uses gcloud-aio-storage (async); lazy import behind
the [gcs] extra. Range reads via the HTTP Range header. NOTE: not locally
integration-tested (no GCP creds in the spike env) — validated by contract parity
with S3 (which IS minio-tested); the conformance suite should run against a real
bucket before relying on it in production (audit §17 follow-up)."""
from __future__ import annotations

from resoluto_sandbox.contracts import ObjectInfo, ObjectStore


class GcsObjectStore(ObjectStore):
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

    async def get(self, key: str, start: int = 0, end: int | None = None) -> bytes:
        headers = None
        if start or end is not None:
            rng = f"bytes={start}-" + ("" if end is None else str(end - 1))
            headers = {"Range": rng}
        return await self._client().download(self._bucket, key, headers=headers)

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

    async def close(self) -> None:
        if self._storage is not None:
            await self._storage.close()
