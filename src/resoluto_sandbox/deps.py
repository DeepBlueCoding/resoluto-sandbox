"""Dependency resolution: decide how to launch a program so its deps are present.

`resolve_invocation(argv, deps, workspace)` returns the actual argv to exec."""
from __future__ import annotations
import re
from pathlib import Path
from typing import Literal, Sequence
from pydantic import BaseModel

_PEP723 = re.compile(r"^#\s*/// script", re.MULTILINE)


class Deps(BaseModel):
    kind: Literal["auto", "inline", "requirements", "image", "vendored"] = "auto"
    requirements: str | None = None  # path (rel to workspace) for kind="requirements"


def _has_pep723(script: Path) -> bool:
    return script.is_file() and script.suffix == ".py" and bool(_PEP723.search(script.read_text()))


def resolve_invocation(argv: Sequence[str], deps: Deps, workspace: Path) -> list[str]:
    """Map (argv, deps) to the argv to actually exec. Inputs: the program argv, a
    Deps strategy, the workspace dir. Output: the launch argv."""
    argv = list(argv)
    kind = deps.kind
    if kind == "auto":
        kind = _detect(argv, workspace)
    if kind == "inline":
        return ["uv", "run", *argv]
    if kind == "requirements":
        req = deps.requirements or "requirements.txt"
        return ["uv", "run", "--with-requirements", str(workspace / req), *argv]
    if kind in ("image", "vendored"):
        return argv
    raise ValueError(f"unknown deps kind {kind!r}")


def _detect(argv: list[str], workspace: Path) -> str:
    first = argv[0] if argv else ""
    script = workspace / first
    if _has_pep723(script):
        return "inline"
    if (workspace / "requirements.txt").is_file():
        return "requirements"
    if (workspace / "pyproject.toml").is_file():
        return "inline"  # uv run uses the project's pyproject
    return "image"  # nothing to install -> run as-is
