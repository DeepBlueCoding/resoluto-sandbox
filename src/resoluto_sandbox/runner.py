"""In-sandbox runner — the passive self-reporting entrypoint.

Runs the node's workload, streams redacted log+span telemetry into the run's
object-store prefix via the ChunkShipper, writes `result.json`, exits. Opens NO
inbound port, holds NO long-lived connection — the orchestrator only ever reads
the store. This is the in-sandbox half; the host half is driver.py.
"""
from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Callable

from resoluto_sandbox.contracts import Conduit, NodeResult
from resoluto_sandbox.spans import SpanEmitter
from resoluto_sandbox.staging import collect_outputs, stage_inputs
from resoluto_sandbox.telemetry import ChunkShipper, result_key


async def _heartbeat(shipper: ChunkShipper, interval_s: float) -> None:
    """Periodically tick the shipper so a chunk lands even when the workload is
    quiet — keeps the reader's liveness signal monotonic, and (since the per-line
    flush was removed) drives timely flushing of buffered output."""
    while True:
        await asyncio.sleep(interval_s)
        await shipper.tick()


async def _exec_logged(em, parent_sid, kind, name, argv, cwd) -> int:
    """Run one command, streaming its merged stdout/stderr as redacted log events
    under its own span, and return the exit code. The unit of in-sandbox work AND
    the lifecycle-hook injection point (setup/workload/cleanup all go through here),
    so every injected step is observable in the span tree."""
    async with em.span(parent_sid, kind, name, inputs={"argv": argv}) as sid:
        proc = await asyncio.create_subprocess_exec(
            *argv, cwd=cwd,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        )
        assert proc.stdout is not None
        async for raw in proc.stdout:
            await em.log(sid, raw.decode("utf-8", "replace").rstrip("\n"))
        return await proc.wait()


async def run_node_in_sandbox(
    *,
    store: Conduit,
    prefix: str,
    run_id: str,
    node_id: str,
    workload_argv: list[str],
    workspace_dir: str | None = None,
    output_paths: list[str] | None = None,
    setup_argv: list[str] | None = None,
    cleanup_argv: list[str] | None = None,
    heartbeat_interval_s: float = 5.0,
    clock: Callable[[], float] = time.time,
    skip_egress_canary: bool = False,
    canary_probe_host: str = "1.1.1.1",
    canary_probe_port: int = 80,
) -> NodeResult:
    """Run one node's workload, self-report telemetry+result to the store.

    Inputs: a Conduit + the run prefix (write-only-scoped in production), the
    node identity, and the workload argv. When `workspace_dir` is set, input
    archives under `<prefix>/inbox/` are staged into it (the repo arrives as
    a store object, never a runtime git-clone) and the workload runs there; on
    success the declared `output_paths` are tarred back to `<prefix>/outbox/`.

    Lifecycle hooks (orchestrator/project-descriptor injectable, observable spans):
      - `setup_argv`  runs BEFORE the workload (inside the node span). Non-zero exit
        aborts the node — a failed setup is a failed node, not a silent skip.
      - `cleanup_argv` runs AFTER the workload, ALWAYS (success, failure, or staging
        error), as a sibling span. This is the "free temp/resources after a gate"
        hook — e.g. `docker builder prune -f`, `docker compose down -v`, `rm -rf
        scratch` — so the tmpfs graph / disk doesn't accrue across steps in a reused
        sandbox. Its own exit code never changes the node verdict (best-effort).

    Returns the `NodeResult` (also written to `<prefix>/result.json`). NOTE the
    verdict here is the OBSERVED exit code — the authoritative gate verdict is still
    derived orchestrator-side; this is work product, not a trust decision.
    """
    shipper = ChunkShipper(store, prefix, clock=clock)
    em = SpanEmitter(shipper, run_id, clock=clock)
    hb = asyncio.ensure_future(_heartbeat(shipper, heartbeat_interval_s))
    result = NodeResult(node_id=node_id)
    try:
        async with em.span("", "node", node_id, inputs={"argv": workload_argv}) as node_sid:
            # Egress canary — platform invariant, runs before setup and workload.
            canary_ok = True
            if skip_egress_canary:
                await em.log(node_sid, "egress canary skipped (trusted-local)")
            else:
                from resoluto_sandbox.egress_canary import run_egress_canary
                async with em.span(node_sid, "egress_canary", "egress_canary") as canary_sid:
                    verdict = await run_egress_canary(
                        store, prefix,
                        probe_host=canary_probe_host,
                        probe_port=canary_probe_port,
                    )
                    for r in verdict.results:
                        await em.log(
                            canary_sid,
                            f"probe {r.target}: passed={r.passed} "
                            f"(expected_reachable={r.expected_reachable}, actual={r.actual_reachable})",
                        )
                    if not verdict.passed:
                        result.status = "failure"
                        result.reason = verdict.reason
                        canary_ok = False

            if canary_ok:
                if workspace_dir is not None:
                    Path(workspace_dir).mkdir(parents=True, exist_ok=True)
                    staged = await stage_inputs(store, prefix, workspace_dir)
                    await em.log(node_sid, f"staged {len(staged)} input archive(s) → {workspace_dir}")
                setup_ok = True
                if setup_argv:
                    src = await _exec_logged(em, node_sid, "setup", "setup", setup_argv, workspace_dir)
                    if src != 0:
                        # a failed setup is a failed node — record it and skip the workload
                        result.exit_code, result.status, setup_ok = src, "failure", False
                        await em.log(node_sid, f"setup hook failed (exit {src}) — skipping workload")
                if setup_ok:
                    rc = await _exec_logged(em, node_sid, "workload", node_id, workload_argv, workspace_dir)
                    result.exit_code = rc
                    result.status = "success" if rc == 0 else "failure"
                    if rc == 0 and workspace_dir is not None and output_paths:
                        result.output_archive = await collect_outputs(store, prefix, workspace_dir, output_paths)
                        await em.log(node_sid, f"collected outputs → {result.output_archive}")
    finally:
        if cleanup_argv:
            try:
                await _exec_logged(em, "", "cleanup", "cleanup", cleanup_argv, workspace_dir)
            except Exception:  # noqa: BLE001 — cleanup is best-effort, never masks the verdict
                pass
        hb.cancel()
        await store.put(result_key(prefix), result.model_dump_json().encode("utf-8"))
        await shipper.close()
    return result
