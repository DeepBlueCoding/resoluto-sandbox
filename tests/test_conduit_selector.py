import pytest

from resoluto.sandbox.runner_main import store_from_env
from resoluto.sandbox.conduit.stdout import StdoutConduit
from resoluto.sandbox.conduit.local import LocalConduit


def test_factory_dispatches_kind_to_conduit_type(tmp_path):
    cases = [
        ({"RESOLUTO_STORE_KIND": "stdout"}, StdoutConduit),
        ({"RESOLUTO_STORE_KIND": "localfs", "RESOLUTO_STORE_ROOT": str(tmp_path)}, LocalConduit),
    ]
    for env, expected in cases:
        assert isinstance(store_from_env(env), expected)


def test_unknown_kind_hard_errors():
    with pytest.raises(RuntimeError):
        store_from_env({"RESOLUTO_STORE_KIND": "redis"})
