"""DI smoke test: a custom Backend subclass is accepted by Sandbox."""
from typing import IO, Sequence

import pytest

from resoluto_sandbox import Backend, RunResult, Sandbox
from resoluto_sandbox.deps import Deps


class _FixedBackend(Backend):
    def __init__(self, result: RunResult) -> None:
        self._result = result

    def run(
        self,
        argv: Sequence[str],
        *,
        workspace=None,
        stdin=None,
        env=None,
        output_paths=None,
        stream=None,
        deps=None,
    ) -> RunResult:
        return self._result


def test_di_backend_is_accepted_and_delegated_to():
    fixed = RunResult(exit_code=0, stdout="injected", stderr="")
    sb = Sandbox(backend=_FixedBackend(fixed))
    result = sb.run(["anything"])
    assert result is fixed
    assert result.stdout == "injected"
    assert result.ok is True


def test_unknown_backend_string_raises():
    with pytest.raises(ValueError, match="unknown backend"):
        Sandbox(backend="nope")
