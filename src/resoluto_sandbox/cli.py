"""Thin CLI for resoluto-sandbox: `run` and `doctor` subcommands."""
from __future__ import annotations

import argparse
import os
import shutil
import sys


def main(argv: list[str] | None = None) -> int:
    """Entry point for resoluto-sandbox CLI. Returns exit code."""
    parser = argparse.ArgumentParser(prog="resoluto-sandbox")
    sub = parser.add_subparsers(dest="cmd")

    run_p = sub.add_parser("run", help="Run a program in a sandbox")
    run_p.add_argument("--backend", default="local", choices=["local", "k8s"])
    run_p.add_argument("--workspace", default=None)
    run_p.add_argument("--image", default=None)
    run_p.add_argument(
        "--deps-kind",
        default=None,
        choices=["auto", "inline", "requirements", "image", "vendored"],
    )
    run_p.add_argument("--requirements", default=None, metavar="PATH")

    sub.add_parser("doctor", help="Check local backend readiness")

    args, rest = parser.parse_known_args(argv)

    if args.cmd == "run":
        return _cmd_run(args, rest)
    if args.cmd == "doctor":
        return _cmd_doctor()
    parser.print_help(sys.stderr)
    return 2


def _cmd_run(args: argparse.Namespace, rest: list[str]) -> int:
    """Handle `run` subcommand. Returns the program's exit code."""
    if "--" in rest:
        idx = rest.index("--")
        program_argv = rest[idx + 1 :]
    else:
        program_argv = []

    if not program_argv:
        print("error: no program specified — use: resoluto-sandbox run [opts] -- <program> [args...]", file=sys.stderr)
        return 2

    from resoluto_sandbox.client import Sandbox
    from resoluto_sandbox.deps import Deps

    if args.deps_kind:
        deps = Deps(kind=args.deps_kind, requirements=args.requirements)
    elif args.requirements:
        deps = Deps(kind="requirements", requirements=args.requirements)
    else:
        deps = None
    if args.backend == "k8s":
        from resoluto_sandbox.backends.k8s import K8sBackend
        sb = Sandbox(backend=K8sBackend(image=args.image))
    else:
        sb = Sandbox(backend=args.backend)
    result = sb.run(program_argv, workspace=args.workspace, deps=deps, stream=sys.stdout)
    return result.exit_code


def _cmd_doctor() -> int:
    """Print a readiness report for the local backend. Returns 0."""
    checks = [
        ("docker", shutil.which("docker") is not None, "needed for k8s/images"),
        ("uv", shutil.which("uv") is not None, "needed for inline deps"),
        ("RESOLUTO_SANDBOX_KUBECONTEXT", "RESOLUTO_SANDBOX_KUBECONTEXT" in os.environ, "needed for k8s"),
    ]
    for label, ok, note in checks:
        status = "OK" if ok else "MISSING"
        print(f"[{status}] {label}  ({note})")
    return 0
