"""Image matrix: build the base + provider-overlay images. The base is tagged by wheel version
(must match at runtime, see version_guard.py); each provider overlay is tagged by its pinned SDK
package + version instead, so the tag itself says what's actually installed. The wheel version
still travels with the overlay as an OCI label + the existing RESOLUTO_IMAGE_VERSION env guard."""
from __future__ import annotations

import os
import subprocess
from importlib.metadata import version as _pkg_version

PROVIDERS = ("claude", "langchain", "openai")

# Where the local backend pulls images from. `docker build` lands an image in the DOCKER daemon's
# store, but the `local` backend runs Kata microVMs against a SEPARATE, dedicated containerd (own
# socket at /run/resoluto-local/containerd/) that can't see Docker's store. The bridge is the on-box
# registry: build → push here → the backend pulls it (localhost is insecure/HTTP by default, so
# `nerdctl run` pulls on demand with no extra flag). Override for k8s / a shared registry; set to
# "" to skip pushing and use bare tags (image must already be loaded into the target containerd).
DEFAULT_REGISTRY = "localhost:5000"


def registry() -> str:
    """The registry the backend pulls images from (RESOLUTO_SANDBOX_REGISTRY, default localhost:5000)."""
    return os.environ.get("RESOLUTO_SANDBOX_REGISTRY", DEFAULT_REGISTRY)


def pullable(tag: str, *, reg: str | None = None) -> str:
    """The registry-qualified reference the backend pulls (e.g. localhost:5000/resoluto-sandbox:...).
    Returns the bare tag unchanged when no registry is configured. Inputs: a bare image tag, optional
    registry override. Output: the pull reference."""
    reg = registry() if reg is None else reg
    return f"{reg}/{tag}" if reg else tag


def _tag_and_push(tag: str, runner) -> str:
    """Tag `tag` for the configured registry and push it, so the local backend can pull it. Inputs:
    a freshly-built bare tag, injectable runner. Output: the pushed pull reference (bare if no registry)."""
    ref = pullable(tag)
    if ref != tag:
        runner(["docker", "tag", tag, ref], check=True)
        runner(["docker", "push", ref], check=True)
    return ref


def _is_registry_qualified(tag: str) -> bool:
    """True if `tag` already names a registry host (e.g. localhost:5000/foo:bar, ghcr.io/x/y)."""
    first = tag.split("/", 1)[0]
    return "/" in tag and ("." in first or ":" in first or first == "localhost")


def push(tag: str, *, runner=subprocess.run) -> str:
    """Publish a locally-built image (e.g. a user's OWN Dockerfile) to the registry the local backend
    pulls from, so `Sandbox(backend='local', image=<ref>)` can run it with no manual containerd load.
    If `tag` is already registry-qualified it's pushed as-is; otherwise it's tagged for the configured
    registry (RESOLUTO_SANDBOX_REGISTRY, default localhost:5000) first. Inputs: a local docker image
    tag, injectable runner. Output: the pull reference to pass as `image=`."""
    if _is_registry_qualified(tag):
        runner(["docker", "push", tag], check=True)
        return tag
    if not registry():
        raise RuntimeError(
            "no registry configured (RESOLUTO_SANDBOX_REGISTRY is empty) — set it, or pass an "
            "already registry-qualified tag like localhost:5000/<name>:<ver>."
        )
    return _tag_and_push(tag, runner)

# The pip package that anchors each overlay's tag, and the version pinned into its Dockerfile.
# Bump SDK_VERSION (and rebuild) to move to a newer SDK release — never a floating install.
SDK_PACKAGE = {"claude": "claude-agent-sdk", "langchain": "langchain", "openai": "openai-agents"}
SDK_VERSION = {"claude": "0.2.110", "langchain": "1.3.11", "openai": "0.17.7"}

# Companion packages/binaries installed alongside a provider's anchor SDK — NOT part of the tag
# (the anchor alone identifies the image), but pinned all the same: an unpinned companion next to
# a pinned anchor is the same reproducibility break, just one line over. Empty dict = no companion.
COMPANION_VERSIONS: dict[str, dict[str, str]] = {
    "claude": {"CLAUDE_CLI_VERSION": "2.1.201"},   # @anthropic-ai/claude-code (npm)
    "langchain": {"LANGGRAPH_VERSION": "1.2.7"},   # langgraph (pip)
    "openai": {},
}


def wheel_version() -> str:
    """The installed resoluto-sandbox version (base image tag + overlay wheel-version label)."""
    return _pkg_version("resoluto-sandbox")


def image_tags(ver: str) -> dict[str, str]:
    """Map of artifact -> tag. Inputs: wheel version (used for the base tag only). Output: tag map;
    each provider tag is its pinned SDK package + version, independent of the wheel version."""
    return {"base": f"resoluto-sandbox-base:{ver}",
            **{p: f"resoluto-sandbox:{SDK_PACKAGE[p]}-{SDK_VERSION[p]}" for p in PROVIDERS}}


def build_base(*, ver: str | None = None, context: str = ".", push: bool = True,
               runner=subprocess.run) -> str:
    """Build the base image and (by default) push it to the registry. Inputs: optional version,
    build context, whether to push, injectable runner. Output: the base image (bare) tag built."""
    ver = ver or wheel_version()
    tag = image_tags(ver)["base"]
    runner(["docker", "build", "-f", "Dockerfile.base", "-t", tag, context], check=True)
    if push:
        _tag_and_push(tag, runner)
    return tag


def build(provider: str, *, ver: str | None = None, context: str = ".", base_tag: str | None = None,
          push: bool = True, runner=subprocess.run) -> str:
    """Build one provider overlay (building base first if needed) and, by default, push it to the
    registry so the local backend can pull it. Output: the overlay's (bare) tag built."""
    if provider not in PROVIDERS:
        raise ValueError(f"unknown provider {provider!r} (expected one of {PROVIDERS})")
    ver = ver or wheel_version()
    tags = image_tags(ver)
    if base_tag is None:
        base_tag = build_base(ver=ver, context=context, push=push, runner=runner)
    tag = tags[provider]
    build_args = [
        "--build-arg", f"BASE_IMAGE={base_tag}",
        "--build-arg", f"IMAGE_VERSION={ver}",
        "--build-arg", f"SDK_VERSION={SDK_VERSION[provider]}",
    ]
    for name, val in COMPANION_VERSIONS[provider].items():
        build_args += ["--build-arg", f"{name}={val}"]
    runner(
        ["docker", "build", "-f", f"images/{provider}.Dockerfile", *build_args, "-t", tag, context],
        check=True,
    )
    if push:
        _tag_and_push(tag, runner)
    return tag
