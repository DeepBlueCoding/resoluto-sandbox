import subprocess
from pathlib import Path

HELPER = Path(__file__).resolve().parents[1] / "scripts" / "dind-graph.sh"


def test_block_graph_rejects_memory_backing_without_creating_image():
    graph = Path("/dev/shm")
    result = subprocess.run(
        ["sh", str(HELPER), str(graph), "block", "134217728"],
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "block graph cannot use tmpfs" in result.stderr


def test_block_graph_rejects_missing_size_before_touching_directory(tmp_path):
    result = subprocess.run(
        ["sh", str(HELPER), str(tmp_path), "block", ""], capture_output=True, text=True
    )
    assert result.returncode != 0
    assert "positive graph capacity required" in result.stderr
    assert list(tmp_path.iterdir()) == []


def test_existing_unknown_image_is_never_reformatted(tmp_path):
    image = tmp_path / ".resoluto-graph.ext4"
    original = b"unrelated-existing-payload"
    image.write_bytes(original)
    result = subprocess.run(
        ["sh", str(HELPER), str(tmp_path), "block", "134217728"],
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "existing graph capacity differs" in result.stderr
    assert image.read_bytes() == original


def test_foreign_graph_content_is_not_hidden_by_new_image(tmp_path):
    original = tmp_path / "existing-layer"
    original.write_bytes(b"existing-runtime-layer")
    result = subprocess.run(
        ["sh", str(HELPER), str(tmp_path), "block", "134217728"],
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "graph directory is not empty" in result.stderr
    assert original.read_bytes() == b"existing-runtime-layer"
    assert list(tmp_path.iterdir()) == [original]


def test_graph_rejects_unknown_backend_before_touching_directory(tmp_path):
    result = subprocess.run(
        ["sh", str(HELPER), str(tmp_path), "unknown", "134217728"],
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "unsupported graph backend" in result.stderr
    assert list(tmp_path.iterdir()) == []
