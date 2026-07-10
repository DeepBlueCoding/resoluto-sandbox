"""Staging round-trip — repo in via inbox/, artifact out via outbox/, traversal-safe."""

import io
import tarfile
from pathlib import Path

import pytest

from resoluto.sandbox.conduit import LocalConduit
from resoluto.sandbox.staging import (
    collect_outputs,
    fetch_outputs,
    put_dir,
    stage_inputs,
)


@pytest.fixture
def store(tmp_path):
    return LocalConduit(tmp_path / "store")


async def test_put_then_stage_round_trips_a_worktree_including_dotgit(store, tmp_path):
    src = tmp_path / "src"
    (src / ".git").mkdir(parents=True)
    (src / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    (src / "README.md").write_text("ORIGINAL\n")

    key = await put_dir(store, "run/r1/nodes/n", str(src))
    assert key == "run/r1/nodes/n/inbox/workspace.tar.gz"

    ws = tmp_path / "ws"
    staged = await stage_inputs(store, "run/r1/nodes/n", str(ws))

    assert staged == [key]
    assert (ws / "README.md").read_text() == "ORIGINAL\n"
    assert (ws / ".git" / "HEAD").read_text() == "ref: refs/heads/main\n"  # history rode along


async def test_paths_scopes_seed_to_task_repos_only(store, tmp_path):
    # A run must see ONLY the repos its task touches — never sibling repos, deps, or the object
    # store that lives alongside them in the workspace root (the umbrella-seeding OOM bug).
    root = tmp_path / "workspace"
    (root / "repoA" / ".git").mkdir(parents=True)
    (root / "repoA" / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    (root / "repoA" / "src.py").write_text("A\n")
    (root / "repoB").mkdir(parents=True)
    (root / "repoB" / "src.py").write_text("B\n")  # sibling repo NOT in the task
    (root / ".resoluto" / "local-store").mkdir(parents=True)
    (root / ".resoluto" / "local-store" / "huge.tar.gz").write_text(
        "prior run archive"
    )  # the store

    await put_dir(store, "run/r1/nodes/n", str(root), paths=["repoA"])

    ws = tmp_path / "ws"
    await stage_inputs(store, "run/r1/nodes/n", str(ws))

    assert (ws / "repoA" / "src.py").read_text() == "A\n"
    assert (ws / "repoA" / ".git" / "HEAD").exists()  # the task repo's history rides along
    assert not (ws / "repoB").exists()  # sibling repo never staged
    assert not (ws / ".resoluto").exists()  # the object store never seeds itself


async def test_excluded_dir_is_dropped_but_protected_path_survives(store, tmp_path):
    # `.claude` is an excluded name, but a repo can TRACK files under it. `protect` must
    # override the exclude for those paths (and their ancestor dirs) so they aren't dropped
    # — a dropped tracked file becomes a phantom deletion downstream.
    src = tmp_path / "src"
    (src / ".claude" / "skills").mkdir(parents=True)
    (src / ".claude" / "skills" / "kept.md").write_text("TRACKED\n")
    (src / ".claude" / "settings.local.json").write_text("untracked junk")
    (src / "node_modules").mkdir()
    (src / "node_modules" / "dep.js").write_text("bloat")
    (src / "README.md").write_text("ORIGINAL\n")

    protect = frozenset({".claude", ".claude/skills", ".claude/skills/kept.md"})
    await put_dir(store, "run/r1/nodes/n", str(src), protect=protect)

    ws = tmp_path / "ws"
    await stage_inputs(store, "run/r1/nodes/n", str(ws))

    assert (
        ws / ".claude" / "skills" / "kept.md"
    ).read_text() == "TRACKED\n"  # protected → survives
    assert not (
        ws / ".claude" / "settings.local.json"
    ).exists()  # unprotected under .claude → dropped
    assert not (ws / "node_modules").exists()  # ordinary exclude still applies
    assert (ws / "README.md").read_text() == "ORIGINAL\n"


async def test_collect_then_fetch_round_trips_declared_outputs(store, tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "patch.diff").write_text("+PATCHED\n")
    (ws / "ignored.bin").write_text("nope")

    await collect_outputs(store, "run/r1/nodes/n", str(ws), ["patch.diff"])

    dest = tmp_path / "out"
    fetched = await fetch_outputs(store, "run/r1/nodes/n", str(dest))

    assert fetched == ["run/r1/nodes/n/outbox/output.tar.gz"]
    assert (dest / "patch.diff").read_text() == "+PATCHED\n"
    assert not (dest / "ignored.bin").exists()  # only declared paths collected


async def test_collect_missing_path_fails_loud(store, tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    with pytest.raises(OSError):  # no fallback — a missing declared output is a real error
        await collect_outputs(store, "run/r1/nodes/n", str(ws), ["does-not-exist"])


async def test_fetch_outputs_neutralizes_path_traversal(store, tmp_path):
    # An ADVERSARIAL guest could craft an output tar with a ../ escape; the host's
    # filtered extract must keep it inside dest (the §12 trust boundary).
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        data = b"pwned"
        info = tarfile.TarInfo("../escape.txt")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    await store.put("run/r1/nodes/n/outbox/evil.tar.gz", buf.getvalue())

    dest = tmp_path / "out"
    with pytest.raises(tarfile.TarError):  # data filter rejects the traversal entry
        await fetch_outputs(store, "run/r1/nodes/n", str(dest))
    assert not (tmp_path / "escape.txt").exists()  # nothing escaped dest


async def test_restage_overwrites_readonly_files(store, tmp_path):
    """Re-staging into a persistent workspace must overwrite read-only collisions —
    git object files are 0444, and a second stage_inputs into the same dir used to
    die with PermissionError at tarfile makefile('wb')."""
    src = tmp_path / "src"
    objects = src / ".git" / "objects" / "e6"
    objects.mkdir(parents=True)
    blob = objects / "9de29bb2d1d6434b8b29ae775ad8c2e48c5391"
    blob.write_bytes(b"blob-v1")
    blob.chmod(0o444)

    await put_dir(store, "run/r1/nodes/n", str(src))
    ws = tmp_path / "ws"
    await stage_inputs(store, "run/r1/nodes/n", str(ws))
    staged_blob = ws / ".git" / "objects" / "e6" / "9de29bb2d1d6434b8b29ae775ad8c2e48c5391"
    assert staged_blob.read_bytes() == b"blob-v1"

    staged = await stage_inputs(store, "run/r1/nodes/n", str(ws))

    assert staged  # second pass extracted cleanly over the read-only tree
    assert staged_blob.read_bytes() == b"blob-v1"
