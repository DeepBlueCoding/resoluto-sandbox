"""Thin CLI for resoluto-sandbox: `run`, `doctor`, and `image` subcommands."""
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
    run_p.add_argument("--env-file", default=None, metavar="PATH",
                       help="dotenv-format file merged into the sandbox env (host-side convenience, "
                            "not a security mechanism — see docs/auth.md for secrets)")

    sub.add_parser("doctor", help="Check local backend readiness")

    image_p = sub.add_parser("image", help="Manage provider images")
    image_sub = image_p.add_subparsers(dest="image_cmd")
    build_p = image_sub.add_parser("build", help="Build base + provider overlay image(s)")
    build_p.add_argument(
        "--provider",
        default="claude",
        choices=["claude", "langchain", "openai", "all"],
    )
    build_p.add_argument("--version", default=None, metavar="VER")
    build_p.add_argument(
        "--context",
        default=".",
        metavar="PATH",
        help="Docker build context path — defaults to this repo's own root (standalone).",
    )

    args, rest = parser.parse_known_args(argv)

    if args.cmd == "run":
        return _cmd_run(args, rest)
    if args.cmd == "doctor":
        return _cmd_doctor()
    if args.cmd == "image":
        return _cmd_image(args)
    parser.print_help(sys.stderr)
    return 2


def _cmd_run(args: argparse.Namespace, rest: list[str]) -> int:
    """Handle `run` subcommand. Returns the program's exit code."""
    if "--" in rest:
        idx = rest.index("--")
        stray = rest[:idx]
        if stray:
            print(f"error: unexpected arguments before '--': {stray}", file=sys.stderr)
            return 2
        program_argv = rest[idx + 1 :]
    else:
        program_argv = []

    if not program_argv:
        print("error: no program specified — use: resoluto-sandbox run [opts] -- <program> [args...]", file=sys.stderr)
        return 2

    from resoluto_sandbox.client import Sandbox

    sb = Sandbox(backend=args.backend, image=args.image)
    result = sb.run(program_argv, workspace=args.workspace, env_file=args.env_file, stream=sys.stdout)
    return result.exit_code


def _doctor_checks() -> list[tuple[str, bool, bool, str]]:
    """Readiness checks as (label, ok, critical, note); critical checks gate the exit code."""
    nerdctl = os.environ.get("RESOLUTO_LOCAL_NERDCTL", "/opt/resoluto-local/bin/nerdctl")
    sock = os.environ.get("RESOLUTO_LOCAL_CONTAINERD_ADDRESS",
                          "/run/resoluto-local/containerd/containerd.sock")
    return [
        ("local: /dev/kvm", os.path.exists("/dev/kvm"), True, "Kata microVMs need KVM"),
        ("local: nerdctl", shutil.which(nerdctl) is not None or os.path.exists(nerdctl), True,
         "container client for the local backend"),
        ("local: dedicated containerd", os.path.exists(sock), True,
         f"run scripts/local-backend-up.sh ({sock})"),
        ("uv", shutil.which("uv") is not None, False, "useful for running Python programs"),
        ("docker", shutil.which("docker") is not None, False, "only needed to build images"),
        ("k8s: RESOLUTO_SANDBOX_KUBECONTEXT", "RESOLUTO_SANDBOX_KUBECONTEXT" in os.environ, False,
         "pinned kube context for the k8s backend"),
    ]


def _cmd_doctor() -> int:
    """Print a local-backend readiness report. Returns 1 if any critical check is MISSING, else 0."""
    checks = _doctor_checks()
    for label, ok, critical, note in checks:
        status = "OK" if ok else ("MISSING" if critical else "absent")
        print(f"[{status}] {label}  ({note})")
    missing = [label for label, ok, critical, _ in checks if critical and not ok]
    if missing:
        print(f"local backend NOT ready — missing: {', '.join(missing)}", file=sys.stderr)
        return 1
    return 0


def _cmd_image(args: argparse.Namespace) -> int:
    """Handle `image` subcommand. Returns exit code."""
    if not hasattr(args, "image_cmd") or args.image_cmd != "build":
        print("error: use `resoluto-sandbox image build`", file=sys.stderr)
        return 2
    import subprocess
    from resoluto_sandbox.images import PROVIDERS, build, build_base
    providers = list(PROVIDERS) if args.provider == "all" else [args.provider]
    context = getattr(args, "context", ".")
    if args.provider == "all":
        prebuilt_base = build_base(ver=args.version, context=context, runner=subprocess.run)
        for p in providers:
            tag = build(p, ver=args.version, context=context, base_tag=prebuilt_base, runner=subprocess.run)
            print(tag)
    else:
        tag = build(providers[0], ver=args.version, context=context, runner=subprocess.run)
        print(tag)
    return 0
