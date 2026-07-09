"""Unit tests for store_from_env.

Verifies that RESOLUTO_STORE_WRITE_TOKEN is preferred over AWS_* when present
(AC: prefers scoped token) and that the fallback to AWS_* still works when the
token is absent (backward compatibility during transition).
"""

import json
import os

import pytest

from resoluto.sandbox.conduit.s3 import S3Conduit
from resoluto.sandbox.runner_main import store_from_env

TOKEN_DICT = {
    "access_key_id": "ASIA_SCOPED_KEY",
    "secret_access_key": "scoped-secret",
    "session_token": "session-tok-xyz",
    "bucket": "resoluto-scoped",
    "endpoint_url": "http://minio:9100",
    "region": "us-east-1",
}


def test_store_from_env_prefers_write_token_over_aws_creds(monkeypatch):
    """When RESOLUTO_STORE_WRITE_TOKEN is set, it must be used — not AWS_*."""
    monkeypatch.setenv("RESOLUTO_STORE_KIND", "s3")
    monkeypatch.setenv("RESOLUTO_STORE_BUCKET", "broad-bucket")
    monkeypatch.setenv("RESOLUTO_STORE_ENDPOINT", "http://minio:9100")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "BROAD_KEY")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "broad-secret")
    monkeypatch.setenv("RESOLUTO_STORE_WRITE_TOKEN", json.dumps(TOKEN_DICT))

    store = store_from_env()

    assert isinstance(store, S3Conduit)
    assert store._bucket == "resoluto-scoped"
    assert store._client_kwargs["aws_access_key_id"] == "ASIA_SCOPED_KEY"
    assert store._client_kwargs["aws_secret_access_key"] == "scoped-secret"
    assert store._client_kwargs["aws_session_token"] == "session-tok-xyz"
    # The BROAD key must NOT appear anywhere in the store's credentials
    assert store._client_kwargs.get("aws_access_key_id") != "BROAD_KEY"


def test_store_from_env_falls_back_to_aws_creds_when_no_token(monkeypatch):
    """Without RESOLUTO_STORE_WRITE_TOKEN, falls back to AWS_* (backward compat)."""
    monkeypatch.setenv("RESOLUTO_STORE_KIND", "s3")
    monkeypatch.setenv("RESOLUTO_STORE_BUCKET", "broad-bucket")
    monkeypatch.setenv("RESOLUTO_STORE_ENDPOINT", "http://minio:9100")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "BROAD_KEY")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "broad-secret")
    monkeypatch.delenv("RESOLUTO_STORE_WRITE_TOKEN", raising=False)

    store = store_from_env()

    assert isinstance(store, S3Conduit)
    assert store._bucket == "broad-bucket"
    assert store._client_kwargs["aws_access_key_id"] == "BROAD_KEY"
    assert store._client_kwargs["aws_secret_access_key"] == "broad-secret"
    assert store._client_kwargs.get("aws_session_token") is None


def test_store_from_env_gcs_rejects_scoped_write_token(monkeypatch):
    """gcs cannot honor a prefix-scoped RESOLUTO_STORE_WRITE_TOKEN — refuse loudly rather than
    silently granting whole-service-account access."""
    monkeypatch.setenv("RESOLUTO_STORE_KIND", "gcs")
    monkeypatch.setenv("RESOLUTO_STORE_BUCKET", "gcs-bucket")
    monkeypatch.setenv("RESOLUTO_STORE_WRITE_TOKEN", json.dumps(TOKEN_DICT))

    with pytest.raises(RuntimeError, match="cannot honor a prefix-scoped"):
        store_from_env()


def test_store_from_env_gcs_builds_without_token(monkeypatch):
    """Without a write token, gcs builds a GcsConduit from bucket + service file (lazy gcloud
    import — no extra needed to construct)."""
    from resoluto.sandbox.conduit.gcs import GcsConduit

    monkeypatch.setenv("RESOLUTO_STORE_KIND", "gcs")
    monkeypatch.setenv("RESOLUTO_STORE_BUCKET", "gcs-bucket")
    monkeypatch.delenv("RESOLUTO_STORE_WRITE_TOKEN", raising=False)

    store = store_from_env()

    assert isinstance(store, GcsConduit)
    assert store._bucket == "gcs-bucket"


def test_store_from_env_localfs_unaffected(monkeypatch, tmp_path):
    """localfs backend is not affected by the RESOLUTO_STORE_WRITE_TOKEN logic."""
    from resoluto.sandbox.conduit import LocalConduit

    monkeypatch.setenv("RESOLUTO_STORE_KIND", "localfs")
    monkeypatch.setenv("RESOLUTO_STORE_ROOT", str(tmp_path))
    monkeypatch.setenv("RESOLUTO_STORE_WRITE_TOKEN", json.dumps(TOKEN_DICT))

    store = store_from_env()

    assert isinstance(store, LocalConduit)
