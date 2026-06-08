"""In-sandbox runner — the passive self-reporting entrypoint (design §7/§13).

Runs the node's workload, streams redacted log+span telemetry into the run's
object-store prefix via the ChunkShipper, writes `result.json`, exits. Opens NO
inbound port, holds NO long-lived connection — the orchestrator only ever reads
the store. This is the in-sandbox half; the host half is driver.py.
"""
from __future__ import annotations

import asyncio
import json
import time
from typing import Callable

from resoluto_sandbox.contracts import ObjectStore
from resoluto_sandbox.spans import SpanEmitter
from resoluto_sandbox.telemetry import ChunkShipper

RESULT_KEY = "result.json"


async def _heartbeat(shipper: ChunkShipper, interval_s: float) -> None:
    """Periodically tick the shipper so a chunk lands even when the workload is
    quiet — keeps the reader's liveness signal monotonic (§11.2)."""
    while True:
        await asyncio.sleep(interval_s)
        await shipper.tick()


async def run_node_in_sandbox(
    *,
    store: ObjectStore,
    prefix: str,
    run_id: str,
    node_id: str,
    workload_argv: list[str],
    heartbeat_interval_s: float = 5.0,
    clock: Callable[[], float] = time.time,
) -> dict:
    """Run one node's workload, self-report telemetry+result to the store.

    Inputs: an ObjectStore + the run prefix (write-only-scoped in production), the
    node identity, and the workload argv. Returns the result dict (also written to
    `<prefix>/result.json`). NOTE the verdict here is the OBSERVED exit code — the
    authoritative gate verdict is still derived orchestrator-side (§12.12); this is
    work product, not a trust decision.
    """
    shipper = ChunkShipper(store, prefix, clock=clock)
    em = SpanEmitter(shipper, run_id, clock=clock)
    hb = asyncio.ensure_future(_heartbeat(shipper, heartbeat_interval_s))
    result: dict = {"node_id": node_id, "status": "failure", "exit_code": None}
    try:
        async with em.span("", "node", node_id, inputs={"argv": workload_argv}) as node_sid:
            proc = await asyncio.create_subprocess_exec(
                *workload_argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            assert proc.stdout is not None
            async for raw in proc.stdout:
                await em.log(node_sid, raw.decode("utf-8", "replace").rstrip("\n"))
                await shipper.flush()  # stream eagerly so the reader tails live
            rc = await proc.wait()
            result["exit_code"] = rc
            result["status"] = "success" if rc == 0 else "failure"
    finally:
        hb.cancel()
        await store.put(f"{prefix.rstrip('/')}/{RESULT_KEY}", json.dumps(result).encode("utf-8"))
        await shipper.close()
    return result
