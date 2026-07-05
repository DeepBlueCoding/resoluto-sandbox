"""Egress canary that verifies egress isolation before an untrusted workload runs."""
from __future__ import annotations

import asyncio

from pydantic import BaseModel

from resoluto.sandbox.contracts import Conduit


class ProbeResult(BaseModel):
    target: str
    expected_reachable: bool
    actual_reachable: bool
    passed: bool


class CanaryVerdict(BaseModel):
    passed: bool
    results: list[ProbeResult]
    reason: str


def evaluate_verdict(results: list[ProbeResult]) -> CanaryVerdict:
    """Reduces a list of ProbeResults to a CanaryVerdict (pass/fail + reason)."""
    failed = [r for r in results if not r.passed]
    if not failed:
        return CanaryVerdict(passed=True, results=results, reason="")
    parts = [
        f"{r.target} (expected_reachable={r.expected_reachable}, actual={r.actual_reachable})"
        for r in failed
    ]
    reason = "egress canary failed: " + ", ".join(parts)
    return CanaryVerdict(passed=False, results=results, reason=reason)


async def probe_tcp(host: str, port: int, timeout_s: float = 3.0) -> bool:
    """Returns True if a TCP connection to host:port succeeds within timeout_s."""
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port),
            timeout=timeout_s,
        )
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
        return True
    except Exception:
        return False


async def probe_store(store: Conduit, prefix: str) -> bool:
    """Returns True if a sentinel PUT to the Conduit succeeds."""
    try:
        await store.put(f"{prefix.rstrip('/')}/_canary_ok", b"ok")
        return True
    except Exception:
        return False


async def run_egress_canary(
    store: Conduit,
    prefix: str,
    probe_host: str = "1.1.1.1",
    probe_port: int = 80,
) -> CanaryVerdict:
    """Runs three probes (external TCP, IMDS TCP, store PUT) and returns the composite CanaryVerdict."""
    external_reachable = await probe_tcp(probe_host, probe_port)
    p_external = ProbeResult(
        target=f"{probe_host}:{probe_port}",
        expected_reachable=False,
        actual_reachable=external_reachable,
        passed=not external_reachable,
    )

    imds_reachable = await probe_tcp("169.254.169.254", 80)
    p_imds = ProbeResult(
        target="169.254.169.254:80",
        expected_reachable=False,
        actual_reachable=imds_reachable,
        passed=not imds_reachable,
    )

    store_ok = await probe_store(store, prefix)
    p_store = ProbeResult(
        target="store",
        expected_reachable=True,
        actual_reachable=store_ok,
        passed=store_ok,
    )

    return evaluate_verdict([p_external, p_imds, p_store])
