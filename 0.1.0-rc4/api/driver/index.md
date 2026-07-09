# Driver

Drive one node through a sandbox end to end: stage its inputs, run it, collect its outputs, and map the raw exit into a typed outcome. `drive_node` is the high-level entry; `drive_node_raw` exposes the un-mapped result; `run_node_in_sandbox` is the low-level single-shot runner.

## resoluto.sandbox.drive_node

```python
drive_node(
    runtime,
    store,
    spec,
    *,
    admit=None,
    on_event=None,
    poll_interval_s=2.0,
    dead_after_s=120.0,
    clock=monotonic,
)
```

Drive one node and read its work product from result.json as a `NodeResult`.

Source code in `src/resoluto/sandbox/driver.py`

```python
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
```

## resoluto.sandbox.drive_node_raw

```python
drive_node_raw(
    runtime,
    store,
    spec,
    *,
    admit=None,
    on_event=None,
    result_ready=None,
    poll_interval_s=2.0,
    dead_after_s=120.0,
    unstartable_polls=15,
    external_gone_polls=15,
    clock=monotonic,
)
```

Launch, tail telemetry, and reap; returns a `NodeOutcome`. Optional `result_ready` completes as soon as the work product appears.

Source code in `src/resoluto/sandbox/driver.py`

```python
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
            if unknown_streak >= external_gone_polls and reader.is_dead():
                return NodeOutcome(
                    disposition="external",
                    observed_phase=phase,
                    reason="pod terminated externally (sustained 'unknown' + telemetry silence)",
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
```

## resoluto.sandbox.NodeOutcome

```python
NodeOutcome(
    disposition,
    observed_phase,
    reason="",
    substrate_logs="",
)
```

Substrate-level disposition of one driven node: 'completed', 'unstartable', 'external', or 'silent'.

## resoluto.sandbox.NodeResult

Bases: `BaseModel`

Typed work product the in-sandbox runner writes to `<prefix>/result.json`.

## resoluto.sandbox.run_node_in_sandbox

```python
run_node_in_sandbox(
    *,
    store,
    prefix,
    run_id,
    node_id,
    workload_argv,
    workspace_dir=None,
    output_paths=None,
    setup_argv=None,
    cleanup_argv=None,
    heartbeat_interval_s=5.0,
    clock=time,
    canary_probe_host="1.1.1.1",
    canary_probe_port=80,
    run_canary=None,
)
```

Run one node's workload (with optional setup/cleanup hooks and input/output staging), self-report telemetry to the store, and return the NodeResult (also written to `<prefix>/result.json`). `run_canary` overrides the egress-isolation canary (tests inject a stub); the canary always runs.

Source code in `src/resoluto/sandbox/runner.py`

```python
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
```
