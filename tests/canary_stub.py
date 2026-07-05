"""Test stub for the in-sandbox egress canary: a passing verdict with no real network probes."""
from resoluto.sandbox.contracts import Conduit
from resoluto.sandbox.egress_canary import CanaryVerdict


async def pass_canary(store: Conduit, prefix: str) -> CanaryVerdict:
    """Inject as run_canary= to skip real TCP probes while keeping the canary span; reports passed."""
    return CanaryVerdict(passed=True, results=[], reason="")
