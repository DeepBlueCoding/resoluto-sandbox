"""In-sandbox runner that runs the node's workload, streams telemetry to the store prefix, and writes result.json."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import TYPE_CHECKING, Awaitable, Callable

from resoluto.sandbox.contracts import Conduit, NodeResult
from resoluto.sandbox.spans import SpanEmitter
from resoluto.sandbox.staging import collect_outputs, stage_inputs
from resoluto.sandbox.telemetry import ChunkShipper, result_key

if TYPE_CHECKING:
    from resoluto.sandbox.egress_canary import CanaryVerdict

CanaryRunner = Callable[[Conduit, str], Awaitable["CanaryVerdict"]]


def _default_canary(probe_host: str, probe_port: int) -> CanaryRunner:
    """Bind the real egress canary to its probe target as a (store, prefix) -> CanaryVerdict callable."""

    async def _run(store: Conduit, prefix: str) -> "CanaryVerdict":
        from resoluto.sandbox.egress_canary import run_egress_canary

        return await run_egress_canary(store, prefix, probe_host=probe_host, probe_port=probe_port)

    return _run


async def _heartbeat(shipper: ChunkShipper, interval_s: float) -> None:
    """Periodically tick the shipper so a chunk lands even when the workload is quiet."""
    while True:
        await asyncio.sleep(interval_s)
        await shipper.tick()


async def _exec_logged(em, parent_sid, kind, name, argv, cwd) -> int:
    """Run one command under its own span, streaming its merged stdout/stderr as log events, and return the exit code."""
    async with em.span(parent_sid, kind, name, inputs={"argv": argv}) as sid:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
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
    canary_probe_host: str = "1.1.1.1",
    canary_probe_port: int = 80,
    run_canary: CanaryRunner | None = None,
) -> NodeResult:
    """Run one node's workload (with optional setup/cleanup hooks and input/output staging), self-report telemetry to the store, and return the NodeResult (also written to `<prefix>/result.json`). `run_canary` overrides the egress-isolation canary (tests inject a stub); the canary always runs."""
    shipper = ChunkShipper(store, prefix, clock=clock)
    em = SpanEmitter(shipper, run_id, clock=clock)
    hb = asyncio.ensure_future(_heartbeat(shipper, heartbeat_interval_s))
    result = NodeResult(node_id=node_id)
    canary = run_canary or _default_canary(canary_probe_host, canary_probe_port)
    try:
        async with em.span("", "node", node_id, inputs={"argv": workload_argv}) as node_sid:
            async with em.span(node_sid, "egress_canary", "egress_canary") as canary_sid:
                verdict = await canary(store, prefix)
                for r in verdict.results:
                    await em.log(
                        canary_sid,
                        f"probe {r.target}: passed={r.passed} "
                        f"(expected_reachable={r.expected_reachable}, actual={r.actual_reachable})",
                    )
                canary_ok = verdict.passed
                if not verdict.passed:
                    result.status = "failure"
                    result.reason = verdict.reason

            if canary_ok:
                if workspace_dir is not None:
                    Path(workspace_dir).mkdir(parents=True, exist_ok=True)
                    staged = await stage_inputs(store, prefix, workspace_dir)
                    await em.log(
                        node_sid, f"staged {len(staged)} input archive(s) → {workspace_dir}"
                    )
                setup_ok = True
                if setup_argv:
                    src = await _exec_logged(
                        em, node_sid, "setup", "setup", setup_argv, workspace_dir
                    )
                    if src != 0:
                        result.exit_code, result.status, setup_ok = src, "failure", False
                        await em.log(
                            node_sid, f"setup hook failed (exit {src}) — skipping workload"
                        )
                if setup_ok:
                    rc = await _exec_logged(
                        em, node_sid, "workload", node_id, workload_argv, workspace_dir
                    )
                    result.exit_code = rc
                    result.status = "success" if rc == 0 else "failure"
                    if rc == 0 and workspace_dir is not None and output_paths:
                        result.output_archive = await collect_outputs(
                            store, prefix, workspace_dir, output_paths
                        )
                        await em.log(node_sid, f"collected outputs → {result.output_archive}")
    finally:
        if cleanup_argv:
            try:
                await _exec_logged(em, "", "cleanup", "cleanup", cleanup_argv, workspace_dir)
            except Exception:  # noqa: BLE001
                pass
        hb.cancel()
        await store.put(result_key(prefix), result.model_dump_json().encode("utf-8"))
        await shipper.close()
    return result
