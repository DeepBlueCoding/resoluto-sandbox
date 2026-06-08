"""Orchestrator lane driver — ties pool + runtime + store-mediated telemetry.

`drive_node` acquires a sandbox from the pool, tails its append-only telemetry
from the object store (forwarding each SpanEvent to `on_event`), and returns the
node's result — distinguishing a clean terminal from a silently-dead substrate
(time-bounded, §11.2/E1/E2). The orchestrator and sandbox never hold a connection;
they rendezvous through the store. Reaping is the lease's (destroy on close)."""
from __future__ import annotations

import asyncio
import json
import time
from typing import Awaitable, Callable

from resoluto_sandbox.contracts import ObjectStore, SandboxLaunchSpec, SpanEvent
from resoluto_sandbox.pool import SandboxPool
from resoluto_sandbox.runner import RESULT_KEY
from resoluto_sandbox.telemetry import ChunkReader

OnEvent = Callable[[SpanEvent], None] | Callable[[SpanEvent], Awaitable[None]]


async def _fire(on_event: OnEvent | None, ev: SpanEvent) -> None:
    if on_event is None:
        return
    r = on_event(ev)
    if asyncio.iscoroutine(r):
        await r


async def drive_node(
    pool: SandboxPool,
    store: ObjectStore,
    spec: SandboxLaunchSpec,
    *,
    on_event: OnEvent | None = None,
    poll_interval_s: float = 2.0,
    dead_after_s: float = 120.0,
    clock: Callable[[], float] = time.time,
) -> dict:
    runtime = pool.runtime
    async with await pool.acquire(spec) as lease:
        reader = ChunkReader(store, spec.store_prefix, dead_after_s=dead_after_s, clock=clock)
        phase = "unknown"
        while True:
            for ev in await reader.poll():
                await _fire(on_event, ev)
            st = await runtime.status(lease.handle)
            phase = st.phase
            if st.terminal:
                for ev in await reader.poll():  # final drain after the pod exits
                    await _fire(on_event, ev)
                break
            if reader.is_dead():
                # silently-dead substrate — the guest can't report its own death,
                # so capture substrate-side forensics here (E2) and fail loud.
                try:
                    logs = await runtime.logs(lease.handle)
                except Exception:  # noqa: BLE001 — forensic best-effort
                    logs = "(unavailable)"
                return {
                    "status": "failure",
                    "reason": "substrate dead — no telemetry within death window",
                    "phase": phase,
                    "substrate_logs": logs[-4000:],
                }
            await asyncio.sleep(poll_interval_s)

        # result.json is the sandbox's WORK PRODUCT; the authoritative gate verdict
        # is derived orchestrator-side from observed signals (§12.12), not trusted here.
        try:
            raw = await store.get(f"{spec.store_prefix.rstrip('/')}/{RESULT_KEY}")
            result = json.loads(raw)
        except Exception:  # noqa: BLE001
            result = {"status": "failure", "reason": "no result.json in store"}
        result.setdefault("phase", phase)
        result["observed_phase"] = phase
        return result
