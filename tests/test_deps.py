from pathlib import Path
from resoluto_sandbox.deps import Deps, resolve_invocation


def test_inline_wraps_with_uv_run(tmp_path):
    assert resolve_invocation(["agent.py"], Deps(kind="inline"), tmp_path) == ["uv", "run", "agent.py"]


def test_image_passthrough(tmp_path):
    assert resolve_invocation(["agent.py"], Deps(kind="image"), tmp_path) == ["agent.py"]


def test_auto_detects_pep723(tmp_path):
    (tmp_path / "a.py").write_text("# /// script\n# dependencies = []\n# ///\nprint(1)")
    assert resolve_invocation(["a.py"], Deps(kind="auto"), tmp_path) == ["uv", "run", "a.py"]


def test_auto_detects_requirements(tmp_path):
    (tmp_path / "requirements.txt").write_text("rich\n")
    out = resolve_invocation(["main.py"], Deps(kind="auto"), tmp_path)
    assert out[:3] == ["uv", "run", "--with-requirements"] and out[-1] == "main.py"


def test_auto_passthrough_when_nothing(tmp_path):
    assert resolve_invocation(["echo", "hi"], Deps(kind="auto"), tmp_path) == ["echo", "hi"]
