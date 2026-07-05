"""Unit tests for egress_canary.evaluate_verdict — pure function, no network."""
import pytest

from resoluto.sandbox.egress_canary import CanaryVerdict, ProbeResult, evaluate_verdict


def _p(target: str, expected: bool, actual: bool) -> ProbeResult:
    return ProbeResult(
        target=target,
        expected_reachable=expected,
        actual_reachable=actual,
        passed=(expected == actual),
    )


def _passing_results() -> list[ProbeResult]:
    return [
        _p("1.1.1.1:80", expected=False, actual=False),   # external blocked ✓
        _p("169.254.169.254:80", expected=False, actual=False),  # IMDS blocked ✓
        _p("store", expected=True, actual=True),           # store reachable ✓
    ]


def test_all_probes_pass_returns_passed_verdict():
    verdict = evaluate_verdict(_passing_results())

    assert verdict.passed is True
    assert verdict.reason == ""
    assert len(verdict.results) == 3


@pytest.mark.parametrize(
    "idx, broken, named",
    [
        (0, _p("1.1.1.1:80", expected=False, actual=True), "1.1.1.1:80"),          # external not blocked
        (1, _p("169.254.169.254:80", expected=False, actual=True), "169.254.169.254:80"),  # IMDS not blocked
        (2, _p("store", expected=True, actual=False), "store"),                    # store unreachable
    ],
)
def test_single_probe_failure_fails_verdict_and_names_target(idx, broken, named):
    results = _passing_results()
    results[idx] = broken

    verdict = evaluate_verdict(results)

    assert verdict.passed is False
    assert "egress" in verdict.reason
    assert named in verdict.reason


def test_multiple_failures_names_all_failed_probes():
    results = [
        _p("1.1.1.1:80", expected=False, actual=True),    # external NOT blocked
        _p("169.254.169.254:80", expected=False, actual=True),  # IMDS NOT blocked
        _p("store", expected=True, actual=True),           # store ok
    ]

    verdict = evaluate_verdict(results)

    assert verdict.passed is False
    assert "1.1.1.1:80" in verdict.reason
    assert "169.254.169.254:80" in verdict.reason
    assert "store" not in verdict.reason
