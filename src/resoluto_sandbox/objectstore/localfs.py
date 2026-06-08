"""LocalFsObjectStore — the dev/CLI backend. Zero infra; the SAME architecture as
cloud (different adapter config). Atomic writes (tmp + rename + fsync) so a chunk
is listable only once fully durable (the §11.2/E3 atomicity invariant)."""
from __future__ import annotations

import os
from pathlib import Path

from resoluto_sandbox.contracts import ObjectInfo, ObjectStore

_TMP_SUFFIX = ".tmp-partial"


class LocalFsObjectStore(ObjectStore):
    def __init__(self, root: str | Path) -> None:
        self._root = Path(root)
        self._root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        # Reject traversal — keys are run/<id>/... never absolute or "..".
        p = (self._root / key).resolve()
        if not str(p).startswith(str(self._root.resolve())):
            raise ValueError(f"key escapes store root: {key!r}")
        return p

    async def put(self, key: str, data: bytes) -> None:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + _TMP_SUFFIX)
        with open(tmp, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)  # atomic; readers never see a partial chunk

    async def get(self, key: str, start: int = 0, end: int | None = None) -> bytes:
        path = self._path(key)
        with open(path, "rb") as f:
            if start:
                f.seek(start)
            if end is None:
                return f.read()
            return f.read(end - start)

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
