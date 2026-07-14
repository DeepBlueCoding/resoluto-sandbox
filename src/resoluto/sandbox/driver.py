"""Launch a sandbox, tail its store-mediated telemetry, and return the node result."""

from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Awaitable, Callable

from pydantic import ValidationError

from resoluto.sandbox.contracts import (
    Admission,
    Conduit,
    ConduitError,
    NodeResult,
    SandboxHandle,
    SandboxLaunchSpec,
    SandboxRuntime,
    SpanEvent,
)
from resoluto.sandbox.telemetry import ChunkReader, result_key

OnEvent = Callable[[SpanEvent], None] | Callable[[SpanEvent], Awaitable[None]]


async def _fire(on_event: OnEvent | None, ev: SpanEvent) -> None:
    if on_event is None:
        return
    r = on_event(ev)
    if asyncio.iscoroutine(r):
        await r


class _DirectLease:
    """Lease for the admission-free path exposing `.handle`."""

    __slots__ = ("handle",)

    def __init__(self, handle: SandboxHandle) -> None:
        self.handle = handle


@asynccontextmanager
async def _direct_lease(runtime: SandboxRuntime, spec: SandboxLaunchSpec):
    """Admission-free launch: launch now, reap on exit."""
    handle = await runtime.launch(spec)
    try:
        yield _DirectLease(handle)
    finally:
        await runtime.destroy(handle)


_FATAL_WAITING = frozenset(
    {
        "ImagePullBackOff",
        "ErrImagePull",
        "ErrImageNeverPull",
        "InvalidImageName",
        "CreateContainerConfigError",
        "CreateContainerError",
        "RunContainerError",
        "CrashLoopBackOff",
    }
)


@dataclass(frozen=True)
class NodeOutcome:
    """Substrate-level disposition of one driven node: 'completed', 'unstartable', 'external', or 'silent'."""

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
    clock: Callable[[], float] = time.monotonic,
) -> NodeOutcome:
    """Launch, tail telemetry, and reap; returns a `NodeOutcome`. Optional `result_ready` completes as soon as the work product appears."""
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
                reader.arm()
            if st.terminal:
                for ev in await reader.poll():
                    await _fire(on_event, ev)
                return NodeOutcome(disposition="completed", observed_phase=phase)
            unstartable_streak = (
                unstartable_streak + 1
                if (phase != "running" and st.reason in _FATAL_WAITING)
                else 0
            )
            if unstartable_streak >= unstartable_polls:
                return NodeOutcome(
                    disposition="unstartable",
                    observed_phase=phase,
                    reason=f"{st.reason} (sustained {unstartable_streak} polls)",
                )
            unknown_streak = unknown_streak + 1 if phase == "unknown" else 0
            # A never-armed reader means the sandbox never reached RUNNING — nothing can
            # be producing telemetry, so a sustained not-found streak is conclusive on its
            # own. Requiring is_dead() here made a sandbox deleted while Pending/gated
            # undetectable FOREVER (is_dead is hard-false until armed).
            if unknown_streak >= external_gone_polls and (reader.is_dead() or not reader.armed):
                return NodeOutcome(
                    disposition="external",
                    observed_phase=phase,
                    reason="sandbox terminated externally (sustained 'unknown' phase)",
                )
            if reader.is_dead():
                try:
                    logs = await runtime.logs(handle)
                except Exception:  # noqa: BLE001
                    logs = "(unavailable)"
                return NodeOutcome(
                    disposition="silent",
                    observed_phase=phase,
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
    clock: Callable[[], float] = time.monotonic,
) -> NodeResult:
    """Drive one node and read its work product from result.json as a `NodeResult`."""
    node_id = spec.labels.get("resoluto.node_id", "")
    outcome = await drive_node_raw(
        runtime,
        store,
        spec,
        admit=admit,
        on_event=on_event,
        poll_interval_s=poll_interval_s,
        dead_after_s=dead_after_s,
        clock=clock,
    )
    if outcome.disposition != "completed":
        return NodeResult(
            node_id=node_id,
            status="failure",
            observed_phase=outcome.observed_phase,
            reason=outcome.reason,
            substrate_logs=outcome.substrate_logs,
        )
    try:
        raw = await store.get(result_key(spec.store_prefix))
    except (ConduitError, OSError):
        return NodeResult(
            node_id=node_id,
            status="failure",
            observed_phase=outcome.observed_phase,
            reason="no result.json in store",
        )
    try:
        result = NodeResult.model_validate_json(raw)
    except ValidationError as e:
        return NodeResult(
            node_id=node_id,
            status="failure",
            observed_phase=outcome.observed_phase,
            reason=f"result.json failed to parse: {e.error_count()} validation error(s)",
        )
    result.observed_phase = outcome.observed_phase
    return result
