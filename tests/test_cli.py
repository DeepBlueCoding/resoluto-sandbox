import sys
from resoluto_sandbox.cli import main


def test_run_streams_and_returns_exit_code(capsys):
    rc = main(["run", "--backend", "local", "--", sys.executable, "-c", "print('cli-ok')"])
    out = capsys.readouterr().out
    assert "cli-ok" in out
    assert rc == 0


def test_run_propagates_nonzero(capsys):
    rc = main(["run", "--backend", "local", "--", sys.executable, "-c", "import sys; sys.exit(7)"])
    assert rc == 7


def test_run_without_program_is_usage_error(capsys):
    rc = main(["run", "--backend", "local"])
    assert rc == 2


def test_doctor_returns_zero(capsys):
    rc = main(["doctor"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "docker" in out.lower() or "uv" in out.lower()


def test_run_stray_args_before_dashdash_is_usage_error(capsys):
    rc = main(["run", "--backend", "local", "junk", "--", sys.executable, "-c", "print(1)"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "unexpected arguments before '--'" in err


def test_run_requirements_flag_builds_deps(monkeypatch):
    from resoluto_sandbox import RunResult
    captured = {}
    class FakeSandbox:
        def __init__(self, **kw): pass
        def run(self, argv, **kw):
            captured["deps"] = kw.get("deps")
            return RunResult(exit_code=0, stdout="", stderr="")
    import resoluto_sandbox.client as _client_mod
    monkeypatch.setattr(_client_mod, "Sandbox", FakeSandbox)
    from resoluto_sandbox.cli import main
    main(["run", "--backend", "local", "--deps-kind", "requirements", "--requirements", "r.txt", "--", "echo", "hi"])
    assert captured["deps"].requirements == "r.txt"
