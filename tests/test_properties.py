from hypothesis import example, given
from hypothesis import strategies as st

from resoluto.sandbox.redact import redact_text

_TOKEN_CHARS = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"


@given(s=st.text())
@example(s="authorization: Bearer abc.def.ghi")
def test_redact_is_idempotent(s):
    once = redact_text(s)
    assert redact_text(once) == once


@given(
    prefix=st.text(),
    suffix=st.text(),
    body=st.text(alphabet=_TOKEN_CHARS, min_size=20, max_size=40),
)
@example(prefix="log: ", suffix=" end", body="A" * 30)
def test_redact_removes_github_token(prefix, suffix, body):
    secret = "ghp_" + body
    raw = f"{prefix} {secret} {suffix}"
    cleaned = redact_text(raw)
    assert secret not in cleaned
    assert redact_text(cleaned) == cleaned
