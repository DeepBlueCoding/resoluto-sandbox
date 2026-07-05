"""Filesystem-backed Conduit with atomic writes (tmp + rename + fsync)."""
from __future__ import annotations

import os
from pathlib import Path

from resoluto.sandbox.contracts import Conduit, ConduitError, ObjectInfo

_TMP_SUFFIX = ".tmp-partial"


class LocalConduit(Conduit):
    def __init__(self, root: str | Path, *, world_writable: bool = False) -> None:
        self._root = Path(root)
        self._world_writable = world_writable
        self._root.mkdir(parents=True, exist_ok=True)
        if world_writable:
            self._chmod_world(self._root)

    @staticmethod
    def _chmod_world(path: Path) -> None:
        try:
            path.chmod(0o777)
        except OSError:
            pass

    def _chmod_tree(self, leaf: Path) -> None:
        d = leaf
        while True:
            self._chmod_world(d)
            if d == self._root or d.parent == d:
                break
            d = d.parent

    @staticmethod
    def _wrap_os_error(exc: OSError) -> ConduitError:
        return ConduitError(f"local object store I/O failed (root={Path(exc.filename).parent if exc.filename else '?'}): {exc}")

    def _path(self, key: str) -> Path:
        root = self._root.resolve()
        p = (self._root / key).resolve()
        if p != root and root not in p.parents:
            raise ValueError(f"key escapes store root: {key!r}")
        return p

    async def put(self, key: str, data: bytes) -> None:
        path = self._path(key)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            if self._world_writable:
                self._chmod_tree(path.parent)
            tmp = path.with_name(path.name + _TMP_SUFFIX)
            with open(tmp, "wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
        except OSError as exc:
            raise self._wrap_os_error(exc) from exc

    async def get(self, key: str) -> bytes:
        try:
            with open(self._path(key), "rb") as f:
                return f.read()
        except OSError as exc:
            raise self._wrap_os_error(exc) from exc

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
                shutil.copy2(self._path(o.key), dst)
                n += 1
        except OSError as exc:
            raise self._wrap_os_error(exc) from exc
        return n
