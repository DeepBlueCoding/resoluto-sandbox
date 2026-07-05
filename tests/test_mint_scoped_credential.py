"""Unit tests for mint_scoped_credential policy generation.

The STS call itself is integration-only; here we test the pure policy builder
`_build_scoped_policy` — the security-critical part. Assertions scan the UNION of
all statements: the builder emits a second statement (prefix-scoped ListBucket),
so inspecting Statement[0] alone would silently miss a granted privilege.
"""
import json

from resoluto.sandbox.conduit.s3 import _build_scoped_policy


def _actions(policy: dict) -> set[str]:
    """Union of every Action across every statement."""
    actions: set[str] = set()
    for stmt in policy["Statement"]:
        actions.update(stmt["Action"])
    return actions


def _statement_with(policy: dict, action: str) -> dict:
    """The single statement granting `action` (fails if absent or ambiguous)."""
    matches = [s for s in policy["Statement"] if action in s["Action"]]
    assert len(matches) == 1, f"expected exactly one statement granting {action}, got {len(matches)}"
    return matches[0]


def test_policy_grants_exactly_put_get_and_scoped_list():
    policy = json.loads(_build_scoped_policy("my-bucket", "run/abc/nodes/n1"))
    assert _actions(policy) == {"s3:PutObject", "s3:GetObject", "s3:ListBucket"}


def test_policy_never_grants_delete_or_acl_or_wildcard():
    policy = json.loads(_build_scoped_policy("my-bucket", "run/abc/nodes/n1"))
    actions = _actions(policy)
    assert "s3:DeleteObject" not in actions
    assert "s3:GetBucketAcl" not in actions
    assert not any(a == "s3:*" or a.endswith(":*") for a in actions)


def test_object_actions_are_scoped_to_bucket_prefix():
    policy = json.loads(_build_scoped_policy("my-bucket", "run/abc/nodes/n1"))
    put = _statement_with(policy, "s3:PutObject")
    assert put["Effect"] == "Allow"
    assert put["Resource"] == "arn:aws:s3:::my-bucket/run/abc/nodes/n1/*"
    # PutObject and GetObject share the one object-scoped statement
    assert set(put["Action"]) == {"s3:PutObject", "s3:GetObject"}


def test_list_bucket_is_constrained_to_the_prefix():
    policy = json.loads(_build_scoped_policy("my-bucket", "run/abc/nodes/n1"))
    lst = _statement_with(policy, "s3:ListBucket")
    assert lst["Effect"] == "Allow"
    assert lst["Resource"] == "arn:aws:s3:::my-bucket"
    # ListBucket on the whole bucket is dangerous unless prefix-conditioned
    assert lst["Condition"]["StringLike"]["s3:prefix"] == ["run/abc/nodes/n1/*"]


def test_different_prefixes_scope_both_object_and_list_statements():
    p1 = json.loads(_build_scoped_policy("bkt", "run/aaa/nodes/n1"))
    p2 = json.loads(_build_scoped_policy("bkt", "run/bbb/nodes/n1"))
    assert _statement_with(p1, "s3:PutObject")["Resource"] != _statement_with(p2, "s3:PutObject")["Resource"]
    assert (
        _statement_with(p1, "s3:ListBucket")["Condition"]["StringLike"]["s3:prefix"]
        != _statement_with(p2, "s3:ListBucket")["Condition"]["StringLike"]["s3:prefix"]
    )


def test_bucket_and_prefix_appear_in_resource():
    policy = json.loads(_build_scoped_policy("resoluto-prod", "run/xyz/nodes/compete"))
    resource = _statement_with(policy, "s3:PutObject")["Resource"]
    assert "resoluto-prod" in resource
    assert "run/xyz/nodes/compete/*" in resource
