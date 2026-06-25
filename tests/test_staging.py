"""Staging round-trip — repo in via inbox/, artifact out via outbox/, traversal-safe."""
import io
import tarfile
from pathlib import Path

import pytest

from resoluto_sandbox.conduit import LocalConduit
from resoluto_sandbox.staging import (
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
