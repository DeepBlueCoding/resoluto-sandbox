"""Secret redaction (§12.7) — applied IN-SANDBOX before any telemetry/log leaves.

Observability is an egress channel; inputs/outputs/logs must never carry secret
env, tokens, Authorization headers, or cred-bearing URLs. Belt-and-suspenders to
the design's primary control (no secret in env/image in the first place)."""
from __future__ import annotations

import re

_SECRET_KEY = re.compile(r"(key|secret|token|password|passwd|cred|auth)", re.I)
_PATTERNS = [
    re.compile(r"(?i)authorization:\s*\S+"),
    re.compile(r"(?i)x-access-token:[^@\s]+"),
    re.compile(r"https?://[^/\s:@]+:[^/\s@]+@"),  # creds in URL userinfo
    re.compile(r"(?i)(?:aws_secret_access_key|aws_session_token)\s*[=:]\s*\S+"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),  # github tokens
    re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\b"),  # JWT
]
_REDACTED = "[REDACTED]"


def redact_text(s: str) -> str:
    for pat in _PATTERNS:
        s = pat.sub(_REDACTED, s)
    return s


def redact_data(data: dict) -> dict:
    out: dict = {}
    for k, v in data.items():
        if _SECRET_KEY.search(k):
            out[k] = _REDACTED
        elif isinstance(v, str):
            out[k] = redact_text(v)
        elif isinstance(v, dict):
            out[k] = redact_data(v)
        elif isinstance(v, list):
            out[k] = [redact_text(x) if isinstance(x, str) else x for x in v]
        else:
            out[k] = v
    return out
