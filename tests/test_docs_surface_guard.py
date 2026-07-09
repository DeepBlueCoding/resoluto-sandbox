"""Durable guard against agent-facing drift.

The 'docker' backend, DockerSandboxRuntime, runtime/docker.py and the RESOLUTO_TRUSTED_LOCAL
bypass were DELETED. The current backends are 'local' (Kata microVM via nerdctl) and 'k8s'.
If the docs/skills/examples ever reference the dead surface again, an agent following them
hard-crashes (`Sandbox(backend="docker")` raises ValueError) or the isolation story regresses.
These tests fail the build if that happens. They scan ONLY the agent-facing surface — src/tests
may legitimately name RESOLUTO_TRUSTED_LOCAL to PROVE it is inert."""

import py_compile
import re
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent

# Files an agent reads to learn the API. NOT src/ or tests/.
_SURFACE_FILES = [_ROOT / n for n in ("AGENTS.md", "llms.txt", "README.md")]
_SURFACE_DIRS = [_ROOT / "examples", _ROOT / "docs", _ROOT / ".claude" / "skills"]

_FORBIDDEN = [
    re.compile(r"""backend\s*=\s*['"]docker['"]"""),
    re.compile(r"\bDockerSandboxRuntime\b"),
    re.compile(r"\bRESOLUTO_TRUSTED_LOCAL\b"),
    re.compile(r"\bDEFAULT_DOCKER_IMAGE\b"),
    re.compile(r"runtime[./]docker\b"),
    re.compile(r"\btest_local_docker_integration\b"),
]
_VALID_BACKENDS = {"local", "k8s"}
_BACKEND_LITERAL = re.compile(r"""backend\s*=\s*['"](\w+)['"]""")


def _surface_text_files():
    for f in _SURFACE_FILES:
        if f.is_file():
            yield f
    for d in _SURFACE_DIRS:
        for p in d.rglob("*"):
            if p.is_file() and p.suffix in {".md", ".txt", ".py", ".json"}:
                yield p


def test_agent_facing_surface_has_no_deleted_docker_backend_references():
    offenders = []
    for path in _surface_text_files():
        text = path.read_text(encoding="utf-8")
        for pat in _FORBIDDEN:
            for m in pat.finditer(text):
                line = text.count("\n", 0, m.start()) + 1
                offenders.append(f"{path.relative_to(_ROOT)}:{line}: matches /{pat.pattern}/")
    assert not offenders, (
        "deleted docker backend / trusted-local bypass resurfaced in agent-facing docs:\n"
        + "\n".join(offenders)
    )


def test_examples_compile_and_use_only_valid_backends():
    examples = sorted((_ROOT / "examples").glob("*.py"))
    assert examples, "no example scripts found — examples/ is the first thing an agent copies"
    for ex in examples:
        py_compile.compile(str(ex), doraise=True)  # raises PyCompileError on syntax rot
        for backend in _BACKEND_LITERAL.findall(ex.read_text(encoding="utf-8")):
            assert backend in _VALID_BACKENDS, (
                f"{ex.name} uses backend={backend!r}; Sandbox accepts only {_VALID_BACKENDS} "
                "(anything else raises ValueError at runtime)"
            )
