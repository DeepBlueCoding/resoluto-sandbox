"""Minimal, dependency-free dotenv-file parser — a host-side convenience for literal config/secret
values, NOT a security mechanism. Values still land as literal env entries in the pod spec / nerdctl
-e args, exactly like env= does today. For values that must never appear in plaintext there, use
SecretKeyRef or a SecretProvider ref instead (see secrets.py)."""

from __future__ import annotations

from pathlib import Path


def parse_env_file(path: str) -> dict[str, str]:
    """Parse a dotenv-format file into a dict. Supports KEY=VALUE lines, optional leading "export ",
    "#" comments, blank lines, and one layer of matching single/double quotes around the value. No
    multiline values, no variable expansion."""
    result: dict[str, str] = {}
    for raw_line in Path(path).read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        line = line.removeprefix("export ").strip()
        key, sep, value = line.partition("=")
        if not sep:
            continue
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        result[key] = value
    return result
