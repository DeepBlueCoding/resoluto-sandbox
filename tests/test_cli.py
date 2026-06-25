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
