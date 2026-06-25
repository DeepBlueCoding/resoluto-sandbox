import pytest
from resoluto_sandbox.version_guard import assert_image_matches_wheel


def test_same_major_minor_different_patch_ok():
    assert_image_matches_wheel("0.2.0", "0.2.5")


def test_different_minor_raises():
    with pytest.raises(RuntimeError, match="drift"):
        assert_image_matches_wheel("0.1.0", "0.2.0")


def test_same_major_minor_without_patch_ok():
    assert_image_matches_wheel("1.0", "1.0.9")


def test_different_major_raises():
    with pytest.raises(RuntimeError, match="drift"):
        assert_image_matches_wheel("2.0.0", "1.0.0")


def test_same_exact_version_ok():
    assert_image_matches_wheel("0.3.1", "0.3.1")
