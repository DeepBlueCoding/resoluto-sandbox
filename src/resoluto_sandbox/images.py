"""Image matrix: build the base + provider-overlay images, version-locked to the wheel."""
from __future__ import annotations

import subprocess
from importlib.metadata import version as _pkg_version

PROVIDERS = ("claude", "langchain", "openai")


def wheel_version() -> str:
    """The installed resoluto-sandbox version (the image tag must match)."""
    return _pkg_version("resoluto-sandbox")


def image_tags(ver: str) -> dict[str, str]:
    """Map of artifact -> tag for a given version. Inputs: version. Output: tag map."""
    return {"base": f"resoluto-sandbox-base:{ver}",
            **{p: f"resoluto-sandbox:{ver}-{p}" for p in PROVIDERS}}


def build(provider: str, *, ver: str | None = None, context: str = ".", base_tag: str | None = None,
          runner=subprocess.run) -> str:
    """Build one provider overlay (building base first if needed). Returns the tag built.
    `runner` is injected for testability (defaults to subprocess.run)."""
    if provider not in PROVIDERS:
        raise ValueError(f"unknown provider {provider!r} (expected one of {PROVIDERS})")
    ver = ver or wheel_version()
    tags = image_tags(ver)
    base = base_tag or tags["base"]
    runner(
        ["docker", "build", "-f", "Dockerfile.base", "-t", base, context],
        check=True,
    )
    tag = tags[provider]
    runner(
        [
            "docker", "build",
            "-f", f"images/{provider}.Dockerfile",
            "--build-arg", f"BASE_IMAGE={base}",
            "--build-arg", f"IMAGE_VERSION={ver}",
            "-t", tag,
            context,
        ],
        check=True,
    )
    return tag
