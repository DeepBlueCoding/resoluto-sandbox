"""Image ENTRYPOINT — the in-sandbox runner, configured entirely from env (§7).

The pod carries NO orchestrator connection; it learns where to self-report from
env the runtime injected (`RESOLUTO_STORE_*`, `RESOLUTO_STORE_PREFIX`) and runs
the workload argv. Exit code mirrors the observed workload status — but the
authoritative gate verdict is still derived orchestrator-side (§12.12)."""
from __future__ import annotations

import asyncio
import json
import os
import sys

from resoluto_sandbox.contracts import ObjectStore
from resoluto_sandbox.runner import run_node_in_sandbox


def store_from_env() -> ObjectStore:
    kind = os.environ["RESOLUTO_STORE_KIND"]
    if kind == "localfs":
        from resoluto_sandbox.objectstore import LocalFsObjectStore

        return LocalFsObjectStore(os.environ["RESOLUTO_STORE_ROOT"])
    if kind == "s3":
        from resoluto_sandbox.objectstore.s3 import S3ObjectStore

        return S3ObjectStore(
            os.environ["RESOLUTO_STORE_BUCKET"],
            endpoint_url=os.environ.get("RESOLUTO_STORE_ENDPOINT") or None,
            region_name=os.environ.get("RESOLUTO_STORE_REGION", "us-east-1"),
            aws_access_key_id=os.environ.get("AWS_ACCESS_KEY_ID"),
            aws_secret_access_key=os.environ.get("AWS_SECRET_ACCESS_KEY"),
        )
    if kind == "gcs":
        from resoluto_sandbox.objectstore.gcs import GcsObjectStore

        return GcsObjectStore(
            os.environ["RESOLUTO_STORE_BUCKET"],
            service_file=os.environ.get("RESOLUTO_GCS_SERVICE_FILE"),
        )
    raise RuntimeError(f"unknown RESOLUTO_STORE_KIND={kind!r}")


async def _main() -> int:
    store = store_from_env()
    output_paths_env = os.environ.get("RESOLUTO_OUTPUT_PATHS")
    result = await run_node_in_sandbox(
        store=store,
        prefix=os.environ["RESOLUTO_STORE_PREFIX"],
        run_id=os.environ["RESOLUTO_RUN_ID"],
        node_id=os.environ["RESOLUTO_NODE_ID"],
        workload_argv=json.loads(os.environ["RESOLUTO_WORKLOAD_ARGV"]),
        workspace_dir=os.environ.get("RESOLUTO_WORKSPACE_DIR"),
        output_paths=json.loads(output_paths_env) if output_paths_env else None,
    )
    return 0 if result.status == "success" else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
