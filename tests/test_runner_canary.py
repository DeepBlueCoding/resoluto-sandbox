"""The default in-guest canary is the egress READINESS gate — not a single-shot
probe racing kube-router (the bootstrap-canary RED regression)."""

from resoluto.sandbox import runner as runner_mod


async def test_default_canary_is_the_readiness_gate(monkeypatch):
    calls = {}

    async def fake_wait(store, prefix, *, probe_host, probe_port):
        calls["args"] = (probe_host, probe_port)
        from resoluto.sandbox.egress_canary import CanaryVerdict

        return CanaryVerdict(passed=True, reason="", results=[])

    monkeypatch.setattr("resoluto.sandbox.egress_canary.wait_for_egress_enforced", fake_wait)
    canary = runner_mod._default_canary("1.1.1.1", 80)
    verdict = await canary(None, "run/x")
    assert verdict.passed is True
    assert calls["args"] == ("1.1.1.1", 80)
