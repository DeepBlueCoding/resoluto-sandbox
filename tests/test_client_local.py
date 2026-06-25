"""Local-backend `Sandbox.run` behavior against real subprocesses."""
import os
import sys

from resoluto_sandbox import RunResult, Sandbox


def test_run_captures_stdout_and_exit_code():
    sb = Sandbox(backend="local")
    out = sb.run([sys.executable, "-c", "print('hello from the box')"])
    assert out.exit_code == 0
    assert out.ok is True
    assert out.stdout.strip() == "hello from the box"
    assert out.stderr == ""


def test_run_propagates_nonzero_exit():
    sb = Sandbox(backend="local")
    out = sb.run([sys.executable, "-c", "import sys; sys.stderr.write('boom\\n'); sys.exit(3)"])
    assert out.exit_code == 3
    assert out.ok is False
    assert "boom" in out.stderr


def test_run_feeds_stdin():
    sb = Sandbox(backend="local")
    out = sb.run([sys.executable, "-c", "import sys; print(sys.stdin.read().upper())"], stdin="abc")
    assert out.stdout.strip() == "ABC"


def test_run_uses_workspace_as_cwd_and_collects_artifacts(tmp_path):
    sb = Sandbox(backend="local")
    out = sb.run(
        [sys.executable, "-c", "open('made.txt','w').write('x')"],
        workspace=str(tmp_path),
        output_paths=["*.txt"],
    )
    assert out.exit_code == 0
    assert (tmp_path / "made.txt").is_file()
    assert any(p.endswith("made.txt") for p in out.artifacts)


def test_run_overlays_env():
    sb = Sandbox(backend="local")
    out = sb.run([sys.executable, "-c", "import os; print(os.environ['ONLY_HERE'])"], env={"ONLY_HERE": "42"})
    assert out.stdout.strip() == "42"


def test_run_surfaces_result_json(tmp_path):
    sb = Sandbox(backend="local")
    out = sb.run(
        [sys.executable, "-c", "open('result.json','w').write('{\"status\": \"success\"}')"],
        workspace=str(tmp_path),
    )
    assert out.result == {"status": "success"}


def test_k8s_backend_constructs():
    sb = Sandbox(backend="k8s", image="example.io/sandbox:latest")
    assert sb._backend == "k8s"
    assert sb._image == "example.io/sandbox:latest"


def test_k8s_run_raises_without_store_kind(monkeypatch):
    for key in list(os.environ):
        if key.startswith("RESOLUTO_STORE_") or key.startswith("AWS_"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.delenv("RESOLUTO_STORE_KIND", raising=False)
    sb = Sandbox(backend="k8s", image="example.io/sandbox:latest")
    try:
        sb.run(["true"])
    except (KeyError, RuntimeError):
        return
    raise AssertionError("expected KeyError or RuntimeError when RESOLUTO_STORE_KIND is absent")


def test_run_image_deps_is_passthrough(tmp_path):
    from resoluto_sandbox.deps import Deps
    out = Sandbox(backend="local").run([sys.executable, "-c", "print('ok')"], deps=Deps(kind="image"))
    assert out.stdout.strip() == "ok"
