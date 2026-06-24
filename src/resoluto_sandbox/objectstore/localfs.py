"""LocalFsObjectStore — the dev/CLI backend. Zero infra; the SAME architecture as
cloud (different adapter config). Atomic writes (tmp + rename + fsync) so a chunk
is listable only once fully durable (the §11.2/E3 atomicity invariant)."""
from __future__ import annotations

import os
from pathlib import Path

from resoluto_sandbox.contracts import ObjectInfo, ObjectStore, ObjectStoreError

_TMP_SUFFIX = ".tmp-partial"


class LocalFsObjectStore(ObjectStore):
    def __init__(self, root: str | Path) -> None:
        self._root = Path(root)
        self._root.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _wrap_os_error(exc: OSError) -> ObjectStoreError:
        # Mirror the S3 adapter: a real I/O failure (disk full, permission, etc.) is a
        # SUBSTRATE failure, not an agent failure — surface it as ObjectStoreError so the lane
        # scaffold translates it to a fatal InfrastructureError (identical attribution to k8s).
        return ObjectStoreError(f"local object store I/O failed (root={Path(exc.filename).parent if exc.filename else '?'}): {exc}")

    def _path(self, key: str) -> Path:
        # Reject traversal — keys are run/<id>/... never absolute or "..".
        p = (self._root / key).resolve()
        if not str(p).startswith(str(self._root.resolve())):
            raise ValueError(f"key escapes store root: {key!r}")
        return p

    async def put(self, key: str, data: bytes) -> None:
        path = self._path(key)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(path.name + _TMP_SUFFIX)
            with open(tmp, "wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)  # atomic; readers never see a partial chunk
        except OSError as exc:
            raise self._wrap_os_error(exc) from exc

    async def get(self, key: str) -> bytes:
        with open(self._path(key), "rb") as f:
            return f.read()

    async def list_prefix(self, prefix: str) -> list[ObjectInfo]:
        base = self._path(prefix)
        if not base.exists():
            return []
        out: list[ObjectInfo] = []
        root = self._root.resolve()
        for p in sorted(base.rglob("*")):
            if p.is_file() and not p.name.endswith(_TMP_SUFFIX):
                out.append(ObjectInfo(key=str(p.resolve().relative_to(root)), size=p.stat().st_size))
        return out

    async def copy_prefix(self, src_prefix: str, dst_prefix: str) -> int:
        import shutil

        src = src_prefix.rstrip("/")
        if not self._path(src).exists():
            return 0
        n = 0
        try:
            for o in await self.list_prefix(src):
                rel = o.key[len(src):].lstrip("/")
                dst = self._path(f"{dst_prefix.rstrip('/')}/{rel}")
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(self._path(o.key), dst)  # path-level — no 184MB buffered into RAM
                n += 1
        except OSError as exc:
            raise self._wrap_os_error(exc) from exc
        return n
