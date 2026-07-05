"""DI smoke test: a custom Backend subclass is accepted by Sandbox."""
from typing import IO, Sequence

import pytest

from resoluto.sandbox import Backend, RunResult, Sandbox
from resoluto.sandbox.secrets import SecretKeyRef


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
        env_file=None,
        secrets=None,
        output_paths=None,
        stream=None,
        egress=None,
    ) -> RunResult:
        self.received = dict(
            argv=list(argv),
            workspace=workspace,
            stdin=stdin,
            env=env,
            env_file=env_file,
            secrets=secrets,
            output_paths=output_paths,
            stream=stream,
            egress=egress,
        )
        return self._result


def test_di_backend_is_accepted_and_delegated_to():
    import sys
    fixed = RunResult(exit_code=0, output="injected", errors="")
    capturing = _CapturingBackend(fixed)
    sb = Sandbox(backend=capturing)
    sentinel_env = {"K": "V"}
    sentinel_secrets = {"ANTHROPIC_API_KEY": SecretKeyRef("anthropic-key", "api_key")}
    result = sb.run(
        ["anything"],
        workspace="/tmp",
        stdin="hi",
        env=sentinel_env,
        env_file=".env",
        secrets=sentinel_secrets,
        output_paths=["*.out"],
        stream=sys.stdout,
    )
    assert result is fixed
    assert result.output == "injected"
    assert result.ok is True
    r = capturing.received
    assert r["argv"] == ["anything"]
    assert r["workspace"] == "/tmp"
    assert r["stdin"] == "hi"
    assert r["env"] == sentinel_env
    assert r["env_file"] == ".env"
    assert r["secrets"] == sentinel_secrets
    assert r["output_paths"] == ["*.out"]
    assert r["stream"] is sys.stdout


def test_unknown_backend_string_raises():
    with pytest.raises(ValueError, match="unknown backend"):
        Sandbox(backend="nope")
