"""DI smoke test: a custom Backend subclass is accepted by Sandbox."""
from typing import IO, Sequence

import pytest

from resoluto_sandbox import Backend, RunResult, Sandbox
from resoluto_sandbox.deps import Deps


class _CapturingBackend(Backend):
    """Captures every kwarg forwarded by Sandbox.run() for assertion."""

    def __init__(self, result: RunResult) -> None:
        self._result = result
        self.received: dict = {}

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
        self.received = dict(
            argv=list(argv),
            workspace=workspace,
            stdin=stdin,
            env=env,
            output_paths=output_paths,
            stream=stream,
            deps=deps,
        )
        return self._result


def test_di_backend_is_accepted_and_delegated_to():
    import sys
    fixed = RunResult(exit_code=0, stdout="injected", stderr="")
    capturing = _CapturingBackend(fixed)
    sb = Sandbox(backend=capturing)
    sentinel_env = {"K": "V"}
    sentinel_deps = Deps(kind="inline")
    result = sb.run(
        ["anything"],
        workspace="/tmp",
        stdin="hi",
        env=sentinel_env,
        output_paths=["*.out"],
        stream=sys.stdout,
        deps=sentinel_deps,
    )
    assert result is fixed
    assert result.stdout == "injected"
    assert result.ok is True
    r = capturing.received
    assert r["argv"] == ["anything"]
    assert r["workspace"] == "/tmp"
    assert r["stdin"] == "hi"
    assert r["env"] == sentinel_env
    assert r["output_paths"] == ["*.out"]
    assert r["stream"] is sys.stdout
    assert r["deps"] is sentinel_deps


def test_unknown_backend_string_raises():
    with pytest.raises(ValueError, match="unknown backend"):
        Sandbox(backend="nope")
