"""Filesystem-backed Conduit with atomic writes (tmp + rename + fsync)."""

from __future__ import annotations

import asyncio
import fcntl
import os
from pathlib import Path

from resoluto.sandbox.contracts import Conduit, ConduitError, ConduitKeyMissing, ObjectInfo

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
        return ConduitError(
            f"local object store I/O failed (root={Path(exc.filename).parent if exc.filename else '?'}): {exc}"
        )

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
        except FileNotFoundError as exc:
            raise ConduitKeyMissing(f"no such key: {key}") from exc
        except OSError as exc:
            raise self._wrap_os_error(exc) from exc

    async def _conditional_put(self, key: str, expected: bytes | None, data: bytes) -> bool:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        lock = path.with_name(path.name + ".conditional-lock")
        with lock.open("ab") as handle:
            while True:
                try:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    await asyncio.sleep(0.01)
            try:
                actual = path.read_bytes() if path.exists() else None
                if actual != expected:
                    return False
                await self.put(key, data)
                return True
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    async def put_if_absent(self, key: str, data: bytes) -> bool:
        return await self._conditional_put(key, None, data)

    async def compare_and_swap(self, key: str, expected: bytes, data: bytes) -> bool:
        return await self._conditional_put(key, expected, data)

    async def list_prefix(self, prefix: str) -> list[ObjectInfo]:
        base = self._path(prefix)
        if not base.exists():
            return []
        out: list[ObjectInfo] = []
        root = self._root.resolve()
        for p in sorted(base.rglob("*")):
            if p.is_file() and not p.name.endswith((_TMP_SUFFIX, ".conditional-lock")):
                out.append(
                    ObjectInfo(key=str(p.resolve().relative_to(root)), size=p.stat().st_size)
                )
        return out

    async def delete_prefix(self, prefix: str) -> int:
        import shutil

        base = self._path(prefix.rstrip("/"))
        if not base.exists():
            return 0
        n = sum(1 for p in base.rglob("*") if p.is_file())
        try:
            shutil.rmtree(base)
        except OSError as exc:
            raise self._wrap_os_error(exc) from exc
        return n

    async def copy_prefix(
        self, src_prefix: str, dst_prefix: str, *, exclude_segments: tuple[str, ...] = ()
    ) -> int:
        import shutil

        src = src_prefix.rstrip("/")
        if not self._path(src).exists():
            return 0
        n = 0
        try:
            for o in await self.list_prefix(src):
                rel = o.key[len(src) :].lstrip("/")
                if exclude_segments and set(rel.split("/")) & set(exclude_segments):
                    continue
                dst = self._path(f"{dst_prefix.rstrip('/')}/{rel}")
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(self._path(o.key), dst)
                n += 1
        except OSError as exc:
            raise self._wrap_os_error(exc) from exc
        return n
