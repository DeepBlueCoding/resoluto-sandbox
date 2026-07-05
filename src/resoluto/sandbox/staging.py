"""Workspace staging over the object store: inputs under `inbox/`, outputs under `outbox/`."""
from __future__ import annotations

import io
import tarfile
from pathlib import Path

from resoluto.sandbox.contracts import Conduit

INBOX = "inbox"
OUTBOX = "outbox"
_ARCHIVE_SUFFIXES = (".tar.gz", ".tgz")

_DEFAULT_EXCLUDES = frozenset({
    ".venv", "venv", "node_modules", "__pycache__", ".mypy_cache", ".pytest_cache",
    ".ruff_cache", ".tox", ".hypothesis", "dist", "build", "htmlcov", ".coverage",
    ".next", ".turbo", ".cache", "resoluto.old", ".claude",
})


def _archive(
    root: Path,
    paths: list[str] | None,
    exclude: frozenset[str] = frozenset(),
    protect: frozenset[str] = frozenset(),
) -> bytes:
    """Tar `root` (or `paths`) to gzip bytes, applying `exclude`/`protect` filters."""
    def _norm(name: str) -> str:
        return name[2:] if name.startswith("./") else name

    def _filter(ti: tarfile.TarInfo) -> tarfile.TarInfo | None:
        if not (_norm(ti.name) in protect) and exclude and exclude.intersection(Path(ti.name).parts):
            return None
        if (ti.issym() or ti.islnk()) and ti.linkname.startswith("/"):
            return None
        return ti

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        if paths is None:
            tar.add(root, arcname=".", filter=_filter)
        else:
            for p in paths:
                tar.add(root / p, arcname=p, filter=_filter)
    return buf.getvalue()


def _extract(data: bytes, dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        tar.extractall(dest, filter="data")


async def put_dir(
    store: Conduit, prefix: str, local_dir: str, *,
    name: str = "workspace", exclude: frozenset[str] = _DEFAULT_EXCLUDES,
    protect: frozenset[str] = frozenset(), paths: list[str] | None = None,
) -> str:
    """Tar `local_dir` and put it under `inbox/`; returns the object key.

    `paths` (each relative to `local_dir`) scopes the archive to just those subtrees. Pass the
    task's repo paths so a lane only ever stages the repos it uses — never the surrounding
    workspace (deps, sibling repos, or the object store itself). `None` = the whole dir."""
    key = f"{prefix.rstrip('/')}/{INBOX}/{name}.tar.gz"
    await store.put(key, _archive(Path(local_dir), paths, exclude, protect))
    return key


async def stage_inputs(store: Conduit, prefix: str, workspace_dir: str) -> list[str]:
    """Extract every input archive under `inbox/` into the workspace; returns the keys staged."""
    dest = Path(workspace_dir)
    staged: list[str] = []
    for info in await store.list_prefix(f"{prefix.rstrip('/')}/{INBOX}"):
        if info.key.endswith(_ARCHIVE_SUFFIXES):
            _extract(await store.get(info.key), dest)
            staged.append(info.key)
    return staged


async def collect_outputs(
    store: Conduit, prefix: str, workspace_dir: str, paths: list[str], *, name: str = "output"
) -> str:
    """Tar the declared output `paths` and put them under `outbox/`; returns the object key."""
    key = f"{prefix.rstrip('/')}/{OUTBOX}/{name}.tar.gz"
    await store.put(key, _archive(Path(workspace_dir), paths))
    return key


async def fetch_outputs(store: Conduit, prefix: str, dest_dir: str) -> list[str]:
    """Extract every output archive under `outbox/` into `dest_dir`; returns the keys fetched."""
    dest = Path(dest_dir)
    fetched: list[str] = []
    for info in await store.list_prefix(f"{prefix.rstrip('/')}/{OUTBOX}"):
        if info.key.endswith(_ARCHIVE_SUFFIXES):
            _extract(await store.get(info.key), dest)
            fetched.append(info.key)
    return fetched
