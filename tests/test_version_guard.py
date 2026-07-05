import pytest

from resoluto.sandbox.version_guard import assert_image_matches_wheel


@pytest.mark.parametrize(
    "image, wheel, ok",
    [
        ("0.2.0", "0.2.5", True),   # same major.minor, differing patch
        ("1.0", "1.0.9", True),     # image lacks patch component
        ("0.3.1", "0.3.1", True),   # exact match
        ("0.1.0", "0.2.0", False),  # minor drift
        ("2.0.0", "1.0.0", False),  # major drift
    ],
)
def test_image_wheel_drift_guard(image, wheel, ok):
    if ok:
        assert_image_matches_wheel(image, wheel)
    else:
        with pytest.raises(RuntimeError, match="drift"):
            assert_image_matches_wheel(image, wheel)
