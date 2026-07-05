"""Image entrypoint that runs the in-sandbox runner, configured entirely from env, exiting 0 on workload success else 1."""
from __future__ import annotations

import asyncio
import json
import os
import sys

from resoluto_sandbox.conduit.factory import store_from_env  # noqa: F401
from resoluto_sandbox.runner import run_node_in_sandbox
from resoluto_sandbox.secrets import secrets_from_env


def _argv_env(name: str) -> list[str] | None:
    raw = os.environ.get(name)
    return json.loads(raw) if raw else None


async def _resolve_secrets() -> None:
    """Resolve any declared SecretProvider refs into os.environ before the workload execs — the
    child subprocess inherits the parent env by default (see runner.py:_exec_logged), so this is
    the entire integration point; no other module needs to know secrets exist."""
    refs_env = os.environ.get("RESOLUTO_SECRET_REFS")
    if not refs_env:
        return
    provider = secrets_from_env()
    if provider is None:
        raise RuntimeError("RESOLUTO_SECRET_REFS is set but RESOLUTO_SECRETS_KIND is not configured")
    for var_name, ref in json.loads(refs_env).items():
        os.environ[var_name] = await provider.get(ref)


async def _main() -> int:
    image_ver = os.environ.get("RESOLUTO_IMAGE_VERSION")
    if image_ver:
        from importlib.metadata import version as _pkg_version
        from resoluto_sandbox.version_guard import assert_image_matches_wheel
        assert_image_matches_wheel(image_ver, _pkg_version("resoluto-sandbox"))
    await _resolve_secrets()
    store = store_from_env()
    output_paths_env = os.environ.get("RESOLUTO_OUTPUT_PATHS")
    canary_port_env = os.environ.get("RESOLUTO_CANARY_PROBE_PORT")
    result = await run_node_in_sandbox(
        store=store,
        prefix=os.environ["RESOLUTO_STORE_PREFIX"],
        run_id=os.environ["RESOLUTO_RUN_ID"],
        node_id=os.environ["RESOLUTO_NODE_ID"],
        workload_argv=json.loads(os.environ["RESOLUTO_WORKLOAD_ARGV"]),
        workspace_dir=os.environ.get("RESOLUTO_WORKSPACE_DIR"),
        output_paths=json.loads(output_paths_env) if output_paths_env else None,
        setup_argv=_argv_env("RESOLUTO_SETUP_ARGV"),
        cleanup_argv=_argv_env("RESOLUTO_CLEANUP_ARGV"),
        canary_probe_host=os.environ.get("RESOLUTO_CANARY_PROBE_HOST", "1.1.1.1"),
        canary_probe_port=int(canary_port_env) if canary_port_env else 80,
    )
    return 0 if result.status == "success" else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
