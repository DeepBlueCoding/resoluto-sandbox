"""Image matrix: build the base + provider-overlay images. The base is tagged by wheel version
(must match at runtime, see version_guard.py); each provider overlay is tagged by its pinned SDK
package + version instead, so the tag itself says what's actually installed. The wheel version
still travels with the overlay as an OCI label + the existing RESOLUTO_IMAGE_VERSION env guard."""
from __future__ import annotations

import subprocess
from importlib.metadata import version as _pkg_version

PROVIDERS = ("claude", "langchain", "openai")

# The pip package that anchors each overlay's tag, and the version pinned into its Dockerfile.
# Bump SDK_VERSION (and rebuild) to move to a newer SDK release — never a floating install.
SDK_PACKAGE = {"claude": "claude-agent-sdk", "langchain": "langchain", "openai": "openai-agents"}
SDK_VERSION = {"claude": "0.2.110", "langchain": "1.3.11", "openai": "0.17.7"}


def wheel_version() -> str:
    """The installed resoluto-sandbox version (base image tag + overlay wheel-version label)."""
    return _pkg_version("resoluto-sandbox")


def image_tags(ver: str) -> dict[str, str]:
    """Map of artifact -> tag. Inputs: wheel version (used for the base tag only). Output: tag map;
    each provider tag is its pinned SDK package + version, independent of the wheel version."""
    return {"base": f"resoluto-sandbox-base:{ver}",
            **{p: f"resoluto-sandbox:{SDK_PACKAGE[p]}-{SDK_VERSION[p]}" for p in PROVIDERS}}


def build_base(*, ver: str | None = None, context: str = ".", runner=subprocess.run) -> str:
    """Build the base image and return its tag. Inputs: optional version, build context,
    injectable runner. Output: the base image tag built."""
    ver = ver or wheel_version()
    tag = image_tags(ver)["base"]
    runner(["docker", "build", "-f", "Dockerfile.base", "-t", tag, context], check=True)
    return tag


def build(provider: str, *, ver: str | None = None, context: str = ".", base_tag: str | None = None,
          runner=subprocess.run) -> str:
    """Build one provider overlay (building base first if needed) and return the tag built."""
    if provider not in PROVIDERS:
        raise ValueError(f"unknown provider {provider!r} (expected one of {PROVIDERS})")
    ver = ver or wheel_version()
    tags = image_tags(ver)
    if base_tag is None:
        base_tag = build_base(ver=ver, context=context, runner=runner)
    tag = tags[provider]
    runner(
        [
            "docker", "build",
            "-f", f"images/{provider}.Dockerfile",
            "--build-arg", f"BASE_IMAGE={base_tag}",
            "--build-arg", f"IMAGE_VERSION={ver}",
            "--build-arg", f"SDK_VERSION={SDK_VERSION[provider]}",
            "-t", tag,
            context,
        ],
        check=True,
    )
    return tag
