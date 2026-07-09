"""Secret redaction for telemetry and logs."""

from __future__ import annotations

import re

_SECRET_KEY = re.compile(r"(key|secret|token|password|passwd|cred|auth)", re.I)
_PATTERNS = [
    re.compile(r"(?i)authorization:\s*\S+"),
    re.compile(r"(?i)x-access-token:[^@\s]+"),
    re.compile(r"https?://[^/\s:@]+:[^/\s@]+@"),
    re.compile(r"(?i)(?:aws_secret_access_key|aws_session_token)\s*[=:]\s*\S+"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\b"),
]
_REDACTED = "[REDACTED]"


def redact_text(s: str) -> str:
    for pat in _PATTERNS:
        s = pat.sub(_REDACTED, s)
    return s


def _redact_value(v):
    if isinstance(v, str):
        return redact_text(v)
    if isinstance(v, dict):
        return redact_data(v)
    if isinstance(v, list):
        return [_redact_value(x) for x in v]
    return v


def redact_data(data: dict) -> dict:
    return {k: _REDACTED if _SECRET_KEY.search(k) else _redact_value(v) for k, v in data.items()}
