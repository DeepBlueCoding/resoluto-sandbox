"""Orchestrator lane driver — ties the substrate runtime + store-mediated telemetry.

`drive_node` launches a sandbox, tails its append-only telemetry from the object store
(forwarding each SpanEvent to `on_event`), and returns the node's result — distinguishing
a clean terminal from a silently-dead substrate (time-bounded). The
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
from dataclasses import dataclass
from typing import Awaitable, Callable

from resoluto_sandbox.contracts import (
    Admission,
    Conduit,
    NodeResult,
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


# Container `waiting.reason`s that mean a pod can NEVER reach running — fail fast instead
# of waiting out the death window (the silence watchdog only arms at RUNNING).
_FATAL_WAITING = frozenset({
    "ImagePullBackOff", "ErrImagePull", "ErrImageNeverPull", "InvalidImageName",
    "CreateContainerConfigError", "CreateContainerError", "RunContainerError", "CrashLoopBackOff",
})


@dataclass(frozen=True)
class NodeOutcome:
    """The SUBSTRATE-level disposition of one driven node — the part that is the same for
    every caller. `disposition`: 'completed' (the pod ran to terminal OR the caller's work
    product appeared), 'unstartable' (a fatal waiting reason), 'external' (vanished after
    running), 'silent' (no telemetry within the death window). Reading the work product
    (result.json — a `NodeResult`, the worker's `StepResult`, …) is the CALLER's concern, so
    the same loop serves every result schema."""

    disposition: str
    observed_phase: str
    reason: str = ""
    substrate_logs: str = ""


async def drive_node_raw(
    runtime: SandboxRuntime,
    store: Conduit,
    spec: SandboxLaunchSpec,
    *,
    admit: Admission | None = None,
    on_event: OnEvent | None = None,
    result_ready: Callable[[], Awaitable[bool]] | None = None,
    poll_interval_s: float = 2.0,
    dead_after_s: float = 120.0,
    unstartable_polls: int = 15,
    external_gone_polls: int = 15,
    clock: Callable[[], float] = time.time,
) -> NodeOutcome:
    """The ONE launch → tail → reap loop with the hardened liveness contract. Returns the
    substrate `NodeOutcome`; the caller reads its own work product. `result_ready` (optional)
    lets a caller whose work product lands before the pod reports terminal (the worker's
    `result.json`) finish as soon as it appears; omit it and completion keys on pod-terminal.

    Liveness: the silence window arms only at RUNNING (Pending/pull/gate time is not silence);
    a fatal `waiting.reason` fails fast (debounced `unstartable_polls`); a vanished-after-running
    pod is reported external only on a SUSTAINED 'unknown' (`external_gone_polls`) AND a dead
    telemetry tail — a single transient 'unknown' on a heartbeating pod never reaps it."""
    lease_cm = (await admit.acquire(spec)) if admit is not None else _direct_lease(runtime, spec)
    async with lease_cm as leased:
        handle = leased.handle
        reader = ChunkReader(store, spec.store_prefix, dead_after_s=dead_after_s, clock=clock)
        phase = "unknown"
        unknown_streak = 0
        unstartable_streak = 0
        while True:
            for ev in await reader.poll():
                await _fire(on_event, ev)
            if result_ready is not None and await result_ready():
                return NodeOutcome(disposition="completed", observed_phase=phase)
            st = await runtime.status(handle)
            phase = st.phase
            if phase == "running":
                # Arm the silence window only at RUNNING (idempotent): Pending/SchedulingGated
                # time — scheduling, image pull, an external admission gate — is not silence.
                reader.arm()
            if st.terminal:
                for ev in await reader.poll():  # final drain after the pod exits
                    await _fire(on_event, ev)
                return NodeOutcome(disposition="completed", observed_phase=phase)
            unstartable_streak = unstartable_streak + 1 if (phase != "running" and st.reason in _FATAL_WAITING) else 0
            if unstartable_streak >= unstartable_polls:
                return NodeOutcome(
                    disposition="unstartable", observed_phase=phase,
                    reason=f"{st.reason} (sustained {unstartable_streak} polls)",
                )
            unknown_streak = unknown_streak + 1 if phase == "unknown" else 0
            if unknown_streak >= external_gone_polls and reader.is_dead():
                return NodeOutcome(
                    disposition="external", observed_phase=phase,
                    reason="pod terminated externally (sustained 'unknown' + telemetry silence)",
                )
            if reader.is_dead():
                # silently-dead substrate — the guest can't report its own death, so capture
                # substrate-side forensics here and fail loud.
                try:
                    logs = await runtime.logs(handle)
                except Exception:  # noqa: BLE001 — forensic best-effort
                    logs = "(unavailable)"
                return NodeOutcome(
                    disposition="silent", observed_phase=phase,
                    reason="substrate dead — no telemetry within death window",
                    substrate_logs=logs[-4000:],
                )
            await asyncio.sleep(poll_interval_s)


async def drive_node(
    runtime: SandboxRuntime,
    store: Conduit,
    spec: SandboxLaunchSpec,
    *,
    admit: Admission | None = None,
    on_event: OnEvent | None = None,
    poll_interval_s: float = 2.0,
    dead_after_s: float = 120.0,
    clock: Callable[[], float] = time.time,
) -> NodeResult:
    """Drive one node and read its work product as a `NodeResult` (the generic contract).
    The launch/tail/reap loop is `drive_node_raw`; this wraps it with `result.json`→NodeResult
    parsing for callers that use the standard result schema."""
    node_id = spec.labels.get("resoluto.node_id", "")
    outcome = await drive_node_raw(
        runtime, store, spec, admit=admit, on_event=on_event,
        poll_interval_s=poll_interval_s, dead_after_s=dead_after_s, clock=clock,
    )
    if outcome.disposition != "completed":
        return NodeResult(
            node_id=node_id, status="failure", observed_phase=outcome.observed_phase,
            reason=outcome.reason, substrate_logs=outcome.substrate_logs,
        )
    # result.json is the sandbox's WORK PRODUCT; the authoritative verdict is derived
    # orchestrator-side from observed signals, not trusted here.
    try:
        result = NodeResult.model_validate_json(await store.get(result_key(spec.store_prefix)))
    except Exception:  # noqa: BLE001 — no/garbled result is itself a failure verdict
        result = NodeResult(node_id=node_id, status="failure", reason="no result.json in store")
    result.observed_phase = outcome.observed_phase
    return result
