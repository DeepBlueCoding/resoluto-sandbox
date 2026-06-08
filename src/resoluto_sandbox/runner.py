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
from pathlib import Path
from typing import Callable

from resoluto_sandbox.contracts import ObjectStore
from resoluto_sandbox.spans import SpanEmitter
from resoluto_sandbox.staging import collect_outputs, stage_inputs
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
    workspace_dir: str | None = None,
    output_paths: list[str] | None = None,
    heartbeat_interval_s: float = 5.0,
    clock: Callable[[], float] = time.time,
) -> dict:
    """Run one node's workload, self-report telemetry+result to the store.

    Inputs: an ObjectStore + the run prefix (write-only-scoped in production), the
    node identity, and the workload argv. When `workspace_dir` is set, input
    archives under `<prefix>/inbox/` are staged into it (§15 — the repo arrives as
    a store object, never a runtime git-clone) and the workload runs there; on
    success the declared `output_paths` are tarred back to `<prefix>/outbox/`.
    Returns the result dict (also written to `<prefix>/result.json`). NOTE the
    verdict here is the OBSERVED exit code — the authoritative gate verdict is still
    derived orchestrator-side (§12.12); this is work product, not a trust decision.
    """
    shipper = ChunkShipper(store, prefix, clock=clock)
    em = SpanEmitter(shipper, run_id, clock=clock)
    hb = asyncio.ensure_future(_heartbeat(shipper, heartbeat_interval_s))
    result: dict = {"node_id": node_id, "status": "failure", "exit_code": None}
    try:
        async with em.span("", "node", node_id, inputs={"argv": workload_argv}) as node_sid:
            if workspace_dir is not None:
                Path(workspace_dir).mkdir(parents=True, exist_ok=True)
                staged = await stage_inputs(store, prefix, workspace_dir)
                await em.log(node_sid, f"staged {len(staged)} input archive(s) → {workspace_dir}")
            proc = await asyncio.create_subprocess_exec(
                *workload_argv,
                cwd=workspace_dir,
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
            if rc == 0 and workspace_dir is not None and output_paths:
                out_key = await collect_outputs(store, prefix, workspace_dir, output_paths)
                result["output_archive"] = out_key
                await em.log(node_sid, f"collected outputs → {out_key}")
    finally:
        hb.cancel()
        await store.put(f"{prefix.rstrip('/')}/{RESULT_KEY}", json.dumps(result).encode("utf-8"))
        await shipper.close()
    return result
