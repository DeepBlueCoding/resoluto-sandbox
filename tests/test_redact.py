"""Redaction proof (§12.7) — secrets must never ride the telemetry channel."""
from resoluto_sandbox.redact import redact_data, redact_text


def test_redact_text_scrubs_known_secret_shapes():
    assert redact_text("Authorization: Bearer abc.def") == "[REDACTED] abc.def"
    assert redact_text("x-access-token:ghp_secret@host")[:10] == "[REDACTED]"
    assert redact_text("https://x-access-token:ghp_aaaaaaaaaaaaaaaaaaaaaa@github.com/r") == "https://[REDACTED]@github.com/r"
    assert redact_text("token ghp_ABCDEFGHIJKLMNOPQRSTUVWX here") == "token [REDACTED] here"
    jwt = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.SflKxwRJSMeKKF2QT4fwpMeJf36"
    assert redact_text(f"jwt {jwt}") == "jwt [REDACTED]"


def test_redact_text_leaves_innocent_text_untouched():
    assert redact_text("building module foo, 12 files compiled") == "building module foo, 12 files compiled"


def test_redact_data_redacts_secret_keys_and_nested_values():
    out = redact_data({
        "argv": ["git", "clone", "https://u:p@github.com/r"],
        "api_token": "supersecret",
        "nested": {"password": "hunter2", "note": "ok"},
        "count": 3,
    })
    assert out["api_token"] == "[REDACTED]"
    assert out["nested"]["password"] == "[REDACTED]"
    assert out["nested"]["note"] == "ok"
    assert out["count"] == 3
    assert out["argv"][2] == "[REDACTED]github.com/r"
