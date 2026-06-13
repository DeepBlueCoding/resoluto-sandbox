"""Workspace staging over the object store (§15 — "tar in the store").

Inputs reach the PASSIVE sandbox as a single archive under `<prefix>/inbox/` —
the ONLY ingress. Default-deny egress forbids a runtime `git clone` (github isn't
allowlisted) and §12.3 forbids creds in the guest, so the repo MUST arrive as a
store object. `.git` rides inside the tar, so history is preserved with zero git
egress. Outputs (e.g. the lane's diff) return under `<prefix>/outbox/`.

tar.gz via stdlib — no external tool, works in the slim runner image. Extraction
is ALWAYS filtered (`data`): the host extracts an OUTPUT tar produced by the
ADVERSARIAL guest, so a path-traversal / absolute-path entry must never escape the
destination. Same filter on the guest side as defense in depth.
"""
from __future__ import annotations

import io
import tarfile
from pathlib import Path

from resoluto_sandbox.contracts import ObjectStore

INBOX = "inbox"
OUTBOX = "outbox"
_ARCHIVE_SUFFIXES = (".tar.gz", ".tgz")

# Dependency / build / cache trees that must NEVER ship in a worktree archive:
# they bloat the tar by orders of magnitude (a Resoluto worktree is ~490MB WITH
# them, a few MB without) AND they hold absolute symlinks (e.g. `.venv/bin/python`
# → /usr/bin/python) that the safe-extract `data` filter rejects with
# AbsoluteLinkError, failing the whole stage. `.git` is deliberately KEPT (history
# travels in the tar; the lane commits with zero git egress).
_DEFAULT_EXCLUDES = frozenset({
    ".venv", "venv", "node_modules", "__pycache__", ".mypy_cache", ".pytest_cache",
    ".ruff_cache", ".tox", ".hypothesis", "dist", "build", "htmlcov", ".coverage",
    ".next", ".turbo", ".cache", "resoluto.old", ".claude",
})


def _archive(root: Path, paths: list[str] | None, exclude: frozenset[str] = frozenset()) -> bytes:
    def _filter(ti: tarfile.TarInfo) -> tarfile.TarInfo | None:
        if exclude and exclude.intersection(Path(ti.name).parts):
            return None
        # An absolute symlink can never be safely re-extracted (AbsoluteLinkError),
        # so dropping it at archive time is the only non-crashing option.
        if (ti.issym() or ti.islnk()) and ti.linkname.startswith("/"):
            return None
        return ti

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        if paths is None:
            tar.add(root, arcname=".", filter=_filter)  # whole worktree incl. .git
        else:
            for p in paths:
                tar.add(root / p, arcname=p, filter=_filter)  # missing path → loud OSError
    return buf.getvalue()


def _extract(data: bytes, dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        tar.extractall(dest, filter="data")  # rejects traversal / absolute / device entries


async def put_dir(
    store: ObjectStore, prefix: str, local_dir: str, *,
    name: str = "workspace", exclude: frozenset[str] = _DEFAULT_EXCLUDES,
) -> str:
    """HOST side: tar a local worktree and PUT it as the sandbox's input. Returns
    the object key. Inputs: store, the lane prefix, the worktree path. Dependency/
    build/cache trees (`exclude`) are dropped — they bloat the archive and carry
    absolute symlinks that break safe extraction."""
    key = f"{prefix.rstrip('/')}/{INBOX}/{name}.tar.gz"
    await store.put(key, _archive(Path(local_dir), None, exclude))
    return key


async def stage_inputs(store: ObjectStore, prefix: str, workspace_dir: str) -> list[str]:
    """SANDBOX side: extract every input archive under `inbox/` into the workspace.
    Returns the keys staged (fail-loud on a corrupt archive)."""
    dest = Path(workspace_dir)
    staged: list[str] = []
    for info in await store.list_prefix(f"{prefix.rstrip('/')}/{INBOX}"):
        if info.key.endswith(_ARCHIVE_SUFFIXES):
            _extract(await store.get(info.key), dest)
            staged.append(info.key)
    return staged


async def collect_outputs(
    store: ObjectStore, prefix: str, workspace_dir: str, paths: list[str], *, name: str = "output"
) -> str:
    """SANDBOX side: tar the declared output paths (relative to the workspace) and
    PUT them under `outbox/`. Returns the object key. A declared path that doesn't
    exist is a contract violation → loud failure."""
    key = f"{prefix.rstrip('/')}/{OUTBOX}/{name}.tar.gz"
    await store.put(key, _archive(Path(workspace_dir), paths))
    return key


async def fetch_outputs(store: ObjectStore, prefix: str, dest_dir: str) -> list[str]:
    """HOST side: extract every output archive under `outbox/` into dest_dir. The
    tar is UNTRUSTED (adversarial guest) — `_extract` is traversal-safe."""
    dest = Path(dest_dir)
    fetched: list[str] = []
    for info in await store.list_prefix(f"{prefix.rstrip('/')}/{OUTBOX}"):
        if info.key.endswith(_ARCHIVE_SUFFIXES):
            _extract(await store.get(info.key), dest)
            fetched.append(info.key)
    return fetched
