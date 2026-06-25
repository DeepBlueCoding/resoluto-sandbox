"""Refuse a baked image whose tag minor doesn't match the installed wheel (drift guard)."""
from __future__ import annotations


def assert_image_matches_wheel(image_version: str, wheel_version: str) -> None:
    """Raise if the image tag's MAJOR.MINOR differs from the wheel's. Inputs: two
    version strings (e.g. '0.2.3'). Output: None; raises RuntimeError on mismatch."""
    def mm(v: str) -> tuple[str, str]:
        parts = v.split(".")
        return (parts[0], parts[1] if len(parts) > 1 else "0")
    if mm(image_version) != mm(wheel_version):
        raise RuntimeError(
            f"image/wheel version drift: image baked at {image_version!r} but wheel is "
            f"{wheel_version!r} (major.minor must match)")
