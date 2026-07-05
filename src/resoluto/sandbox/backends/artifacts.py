"""Shared artifact helpers for backends: glob-collect outputs and read result.json."""
from __future__ import annotations

import glob
import json
from pathlib import Path
from typing import Sequence

from resoluto.sandbox.telemetry import RESULT_FILENAME


def _collect(cwd: Path, output_paths: Sequence[str] | None) -> list[str]:
    """Resolve ``output_paths`` globs under ``cwd`` into a sorted list of paths."""
    if not output_paths:
        return []
    found: list[str] = []
    for pattern in output_paths:
        found.extend(sorted(glob.glob(str(cwd / pattern), recursive=True)))
    return found


def read_result_json(cwd: Path) -> dict | None:
    """Return the parsed result.json under ``cwd``, or None when absent."""
    path = cwd / RESULT_FILENAME
    if not path.is_file():
        return None
    return json.loads(path.read_text())
