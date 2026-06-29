import pytest

import resoluto_sandbox.cli as cli
from resoluto_sandbox.cli import main


@pytest.mark.integration
def test_run_streams_and_returns_exit_code(capsys):
    rc = main([
        "run", "--backend", "local", "--image", "localhost:5000/resoluto-lane:dev",
        "--", "python", "-c", "print('cli-ok')",
    ])
    out = capsys.readouterr().out
    assert "cli-ok" in out
    assert rc == 0


@pytest.mark.integration
def test_run_propagates_nonzero(capsys):
    rc = main([
        "run", "--backend", "local", "--image", "localhost:5000/resoluto-lane:dev",
        "--", "python", "-c", "import sys; sys.exit(7)",
    ])
    assert rc == 7


@pytest.mark.parametrize(
    "argv, err_contains",
    [
        (["run", "--backend", "local"], None),                                   # no program
        (["run", "--backend", "local", "junk", "--", "python", "-c", "x"],       # stray args
         "unexpected arguments before '--'"),
    ],
)
def test_run_usage_errors(capsys, argv, err_contains):
    rc = main(argv)
    assert rc == 2
    if err_contains:
        assert err_contains in capsys.readouterr().err


def test_doctor_reports_ready_and_exits_zero(monkeypatch, capsys):
    monkeypatch.setattr(cli.os.path, "exists", lambda p: True)
    monkeypatch.setattr(cli.shutil, "which", lambda x: f"/usr/bin/{x}")
    rc = main(["doctor"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "local: /dev/kvm" in out and "[OK]" in out


def test_doctor_exits_nonzero_when_local_backend_not_ready(monkeypatch, capsys):
    # no kvm, no nerdctl, no dedicated containerd → critical checks MISSING
    monkeypatch.setattr(cli.os.path, "exists", lambda p: False)
    monkeypatch.setattr(cli.shutil, "which", lambda x: None)
    rc = main(["doctor"])
    err = capsys.readouterr().err
    assert rc == 1
    assert "local backend NOT ready" in err
