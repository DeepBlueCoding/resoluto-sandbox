"""Unit tests for egress_canary.evaluate_verdict — pure function, no network."""
import pytest

from resoluto_sandbox.egress_canary import CanaryVerdict, ProbeResult, evaluate_verdict


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


def test_non_allowlisted_reachable_fails_with_egress_in_reason():
    results = _passing_results()
    results[0] = _p("1.1.1.1:80", expected=False, actual=True)  # egress NOT blocked

    verdict = evaluate_verdict(results)

    assert verdict.passed is False
    assert "egress" in verdict.reason
    assert "1.1.1.1:80" in verdict.reason


def test_imds_reachable_fails_verdict():
    results = _passing_results()
    results[1] = _p("169.254.169.254:80", expected=False, actual=True)

    verdict = evaluate_verdict(results)

    assert verdict.passed is False
    assert "egress" in verdict.reason
    assert "169.254.169.254:80" in verdict.reason


def test_store_unreachable_fails_verdict():
    results = _passing_results()
    results[2] = _p("store", expected=True, actual=False)

    verdict = evaluate_verdict(results)

    assert verdict.passed is False
    assert "egress" in verdict.reason
    assert "store" in verdict.reason


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
