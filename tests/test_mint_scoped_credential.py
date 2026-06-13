"""Unit tests for mint_scoped_credential policy generation.

The STS call itself is integration-only; here we test the pure policy builder
`_build_scoped_policy` which is the security-critical part.
"""
import json

import pytest

from resoluto_sandbox.objectstore.s3 import _build_scoped_policy


def test_policy_actions_are_put_and_get_only():
    policy = json.loads(_build_scoped_policy("my-bucket", "run/abc/nodes/n1"))
    stmt = policy["Statement"][0]
    assert set(stmt["Action"]) == {"s3:PutObject", "s3:GetObject"}


def test_policy_resource_is_prefix_scoped():
    policy = json.loads(_build_scoped_policy("my-bucket", "run/abc/nodes/n1"))
    stmt = policy["Statement"][0]
    assert stmt["Resource"] == "arn:aws:s3:::my-bucket/run/abc/nodes/n1/*"


def test_policy_effect_is_allow():
    policy = json.loads(_build_scoped_policy("my-bucket", "run/abc/nodes/n1"))
    stmt = policy["Statement"][0]
    assert stmt["Effect"] == "Allow"


def test_policy_does_not_grant_list_or_delete():
    policy = json.loads(_build_scoped_policy("my-bucket", "run/abc/nodes/n1"))
    stmt = policy["Statement"][0]
    actions = set(stmt["Action"])
    assert "s3:DeleteObject" not in actions
    assert "s3:ListBucket" not in actions
    assert "s3:GetBucketAcl" not in actions


def test_policy_different_prefixes_produce_different_resources():
    p1 = json.loads(_build_scoped_policy("bkt", "run/aaa/nodes/n1"))
    p2 = json.loads(_build_scoped_policy("bkt", "run/bbb/nodes/n1"))
    assert p1["Statement"][0]["Resource"] != p2["Statement"][0]["Resource"]


def test_policy_bucket_in_resource():
    policy = json.loads(_build_scoped_policy("resoluto-prod", "run/xyz/nodes/compete"))
    resource = policy["Statement"][0]["Resource"]
    assert "resoluto-prod" in resource
    assert "run/xyz/nodes/compete/*" in resource
