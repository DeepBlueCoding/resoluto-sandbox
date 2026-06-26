import pytest

from resoluto_sandbox.cli import main


@pytest.mark.integration
def test_run_streams_and_returns_exit_code(capsys):
    rc = main([
        "run", "--backend", "local", "--image", "resoluto-sandbox-base:0.1.0",
        "--", "python", "-c", "print('cli-ok')",
    ])
    out = capsys.readouterr().out
    assert "cli-ok" in out
    assert rc == 0


@pytest.mark.integration
def test_run_propagates_nonzero(capsys):
    rc = main([
        "run", "--backend", "local", "--image", "resoluto-sandbox-base:0.1.0",
        "--", "python", "-c", "import sys; sys.exit(7)",
    ])
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
    rc = main(["run", "--backend", "local", "junk", "--", "python", "-c", "print(1)"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "unexpected arguments before '--'" in err
