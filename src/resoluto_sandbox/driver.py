"""Orchestrator lane driver — ties the substrate runtime + store-mediated telemetry.

`drive_node` launches a sandbox, tails its append-only telemetry from the object store
(forwarding each SpanEvent to `on_event`), and returns the node's result — distinguishing
a clean terminal from a silently-dead substrate (time-bounded, §11.2/E1/E2). The
orchestrator and sandbox never hold a connection; they rendezvous through the store.

ADMISSION is decoupled and OPTIONAL: pass `admit` (any `Admission` — e.g. the in-process
`SandboxPool`) to gate WHEN the pod launches; pass nothing and the pod launches
immediately (the right shape when an EXTERNAL admitter like Kueue, or the plain
kube-scheduler, already gates it via the spec's pod metadata). The substrate never
depends on any admitter."""
from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager
from typing import Awaitable, Callable

from resoluto_sandbox.contracts import (
    Admission,
    NodeResult,
    ObjectStore,
    SandboxHandle,
    SandboxLaunchSpec,
    SandboxRuntime,
    SpanEvent,
)
from resoluto_sandbox.telemetry import ChunkReader, result_key

OnEvent = Callable[[SpanEvent], None] | Callable[[SpanEvent], Awaitable[None]]


async def _fire(on_event: OnEvent | None, ev: SpanEvent) -> None:
    if on_event is None:
        return
    r = on_event(ev)
    if asyncio.iscoroutine(r):
        await r


class _DirectLease:
    """Minimal Lease for the admission-free path — just exposes `.handle` so the driver
    reads it uniformly whether the slot came from an Admission or a direct launch."""

    __slots__ = ("handle",)

    def __init__(self, handle: SandboxHandle) -> None:
        self.handle = handle


@asynccontextmanager
async def _direct_lease(runtime: SandboxRuntime, spec: SandboxLaunchSpec):
    """Admission-free launch: launch now, reap on exit — the no-admitter path (an external
    scheduler/Kueue, or nothing, decides WHEN; the substrate just does the HOW)."""
    handle = await runtime.launch(spec)
    try:
        yield _DirectLease(handle)
    finally:
        await runtime.destroy(handle)


async def drive_node(
    runtime: SandboxRuntime,
    store: ObjectStore,
    spec: SandboxLaunchSpec,
    *,
    admit: Admission | None = None,
    on_event: OnEvent | None = None,
    poll_interval_s: float = 2.0,
    dead_after_s: float = 120.0,
    clock: Callable[[], float] = time.time,
) -> NodeResult:
    node_id = spec.labels.get("resoluto.node_id", "")
    lease_cm = (await admit.acquire(spec)) if admit is not None else _direct_lease(runtime, spec)
    async with lease_cm as leased:
        handle = leased.handle
        reader = ChunkReader(store, spec.store_prefix, dead_after_s=dead_after_s, clock=clock)
        phase = "unknown"
        while True:
            for ev in await reader.poll():
                await _fire(on_event, ev)
            st = await runtime.status(handle)
            prev_phase, phase = phase, st.phase
            if st.terminal:
                for ev in await reader.poll():  # final drain after the pod exits
                    await _fire(on_event, ev)
                break
            if phase == "running":
                # Arm the silence window only at RUNNING (idempotent): Pending/SchedulingGated
                # time — scheduling, image pull, an external admission gate — is not silence.
                reader.arm()
            if prev_phase == "running" and phase == "unknown":
                # The pod vanished after running — an EXTERNAL termination (evicted,
                # preempted, node-drained, deleted), not a silent substrate death. Report
                # it as such instead of waiting out the death window.
                return NodeResult(
                    node_id=node_id, status="failure", observed_phase=phase,
                    reason="pod terminated externally (evicted/deleted) while running",
                )
            if reader.is_dead():
                # silently-dead substrate — the guest can't report its own death,
                # so capture substrate-side forensics here (E2) and fail loud.
                try:
                    logs = await runtime.logs(handle)
                except Exception:  # noqa: BLE001 — forensic best-effort
                    logs = "(unavailable)"
                return NodeResult(
                    node_id=node_id,
                    status="failure",
                    observed_phase=phase,
                    reason="substrate dead — no telemetry within death window",
                    substrate_logs=logs[-4000:],
                )
            await asyncio.sleep(poll_interval_s)

        # result.json is the sandbox's WORK PRODUCT; the authoritative gate verdict
        # is derived orchestrator-side from observed signals (§12.12), not trusted here.
        try:
            result = NodeResult.model_validate_json(await store.get(result_key(spec.store_prefix)))
        except Exception:  # noqa: BLE001 — no/garbled result is itself a failure verdict
            result = NodeResult(node_id=node_id, status="failure", reason="no result.json in store")
        result.observed_phase = phase
        return result
