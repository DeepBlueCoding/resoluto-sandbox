import pytest
from resoluto_sandbox.runner_main import store_from_env
from resoluto_sandbox.conduit.stdout import StdoutConduit
from resoluto_sandbox.conduit.local import LocalConduit


def test_stdout_kind():
    c = store_from_env({"RESOLUTO_STORE_KIND": "stdout"})
    assert isinstance(c, StdoutConduit)


def test_localfs_kind(tmp_path):
    c = store_from_env({"RESOLUTO_STORE_KIND": "localfs", "RESOLUTO_STORE_ROOT": str(tmp_path)})
    assert isinstance(c, LocalConduit)


def test_unknown_kind_hard_errors():
    with pytest.raises(RuntimeError):
        store_from_env({"RESOLUTO_STORE_KIND": "redis"})
