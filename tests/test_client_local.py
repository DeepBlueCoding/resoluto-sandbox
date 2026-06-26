"""Local-backend `Sandbox.run` behavior against real subprocesses."""
import os
import sys

from resoluto_sandbox import RunResult, Sandbox


def test_run_captures_stdout_and_exit_code():
    sb = Sandbox(backend="local")
    out = sb.run([sys.executable, "-c", "print('hello from the box')"])
    assert out.exit_code == 0
    assert out.ok is True
    assert out.output.strip() == "hello from the box"
    assert out.errors == ""


def test_run_propagates_nonzero_exit():
    sb = Sandbox(backend="local")
    out = sb.run([sys.executable, "-c", "import sys; sys.stderr.write('boom\\n'); sys.exit(3)"])
    assert out.exit_code == 3
    assert out.ok is False
    assert "boom" in out.errors


def test_run_feeds_stdin():
    sb = Sandbox(backend="local")
    out = sb.run([sys.executable, "-c", "import sys; print(sys.stdin.read().upper())"], stdin="abc")
    assert out.output.strip() == "ABC"


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
    assert out.output.strip() == "42"


def test_run_surfaces_result_json(tmp_path):
    sb = Sandbox(backend="local")
    out = sb.run(
        [sys.executable, "-c", "open('result.json','w').write('{\"status\": \"success\"}')"],
        workspace=str(tmp_path),
    )
    assert out.result == {"status": "success"}


def test_k8s_backend_constructs():
    from resoluto_sandbox.backends.k8s import K8sBackend
    from resoluto_sandbox.backends.local import LocalBackend
    sb_k8s = Sandbox(backend=K8sBackend(image="example.io/sandbox:latest"))
    assert isinstance(sb_k8s._backend, K8sBackend)
    sb_local = Sandbox(backend="local")
    assert isinstance(sb_local._backend, LocalBackend)


def test_k8s_run_raises_without_store_kind(monkeypatch):
    from resoluto_sandbox.backends.k8s import K8sBackend
    for key in list(os.environ):
        if key.startswith("RESOLUTO_STORE_") or key.startswith("AWS_"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.delenv("RESOLUTO_STORE_KIND", raising=False)
    sb = Sandbox(backend=K8sBackend(image="example.io/sandbox:latest"))
    try:
        sb.run(["true"])
    except (KeyError, RuntimeError):
        return
    raise AssertionError("expected KeyError or RuntimeError when RESOLUTO_STORE_KIND is absent")


def test_run_survives_child_closing_stdin_early():
    sb = Sandbox(backend="local")
    out = sb.run(
        [sys.executable, "-c", "import sys; sys.stdin.close()"],
        stdin="x" * 100000,
    )
    assert out.exit_code == 0
    assert isinstance(out, RunResult)
