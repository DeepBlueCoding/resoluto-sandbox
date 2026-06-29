"""Factory for building a Conduit from environment variables."""
from __future__ import annotations

import json
import os

from resoluto_sandbox.contracts import Conduit


def store_from_env(env: dict[str, str] | None = None) -> Conduit:
    """Build a Conduit from environment variables. Inputs: optional env dict (defaults
    to os.environ). Output: a concrete Conduit for the requested RESOLUTO_STORE_KIND."""
    env = env if env is not None else os.environ
    kind = env["RESOLUTO_STORE_KIND"]
    if kind == "stdout":
        from resoluto_sandbox.conduit.stdout import StdoutConduit

        return StdoutConduit()
    if kind == "localfs":
        from resoluto_sandbox.conduit import LocalConduit

        return LocalConduit(env["RESOLUTO_STORE_ROOT"])
    if kind == "s3":
        from resoluto_sandbox.conduit.s3 import S3Conduit

        write_token = env.get("RESOLUTO_STORE_WRITE_TOKEN")
        if write_token:
            tok = json.loads(write_token)
            return S3Conduit(
                tok["bucket"],
                endpoint_url=tok.get("endpoint_url"),
                region_name=tok.get("region", "us-east-1"),
                aws_access_key_id=tok["access_key_id"],
                aws_secret_access_key=tok["secret_access_key"],
                aws_session_token=tok.get("session_token"),
            )
        return S3Conduit(
            env["RESOLUTO_STORE_BUCKET"],
            endpoint_url=env.get("RESOLUTO_STORE_ENDPOINT") or None,
            region_name=env.get("RESOLUTO_STORE_REGION", "us-east-1"),
            aws_access_key_id=env.get("AWS_ACCESS_KEY_ID"),
            aws_secret_access_key=env.get("AWS_SECRET_ACCESS_KEY"),
        )
    if kind == "gcs":
        from resoluto_sandbox.conduit.gcs import GcsConduit

        return GcsConduit(
            env["RESOLUTO_STORE_BUCKET"],
            service_file=env.get("RESOLUTO_GCS_SERVICE_FILE"),
        )
    raise RuntimeError(f"unknown RESOLUTO_STORE_KIND={kind!r}")
