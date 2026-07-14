# Telemetry

The self-reporting channel: the guest ships immutable JSONL chunks to the conduit (`ChunkShipper`) and the host tails them back (`ChunkReader`). `SpanEmitter` records structured `SpanEvent` spans over the run.

## resoluto.sandbox.ChunkShipper

```python
ChunkShipper(
    store,
    prefix,
    *,
    flush_bytes=64 * 1024,
    flush_interval_s=5.0,
    heartbeat_s=30.0,
    heartbeat_factory=_default_heartbeat,
    clock=time,
)
```

In-sandbox writer that buffers JSONL lines and flushes immutable chunks to the store, emitting a heartbeat when quiet.

Source code in `src/resoluto/sandbox/telemetry.py`

```python
def __init__(
    self,
    store: Conduit,
    prefix: str,
    *,
    flush_bytes: int = 64 * 1024,
    flush_interval_s: float = 5.0,
    heartbeat_s: float = 30.0,
    heartbeat_factory: Callable[[float], str] = _default_heartbeat,
    clock: Callable[[], float] = time.time,
) -> None:
    self._store = store
    self._prefix = prefix.rstrip("/")
    self._flush_bytes = flush_bytes
    self._flush_interval_s = flush_interval_s
    self._heartbeat_s = heartbeat_s
    self._heartbeat_factory = heartbeat_factory
    self._clock = clock
    self._buf: list[str] = []
    self._buf_bytes = 0
    self._index = 0
    self._last_flush = clock()
    self._closed = False
    self._flush_lock = asyncio.Lock()
```

### emit_line

```python
emit_line(line)
```

Buffer one JSONL record, flushing if the byte threshold is reached.

Source code in `src/resoluto/sandbox/telemetry.py`

```python
async def emit_line(self, line: str) -> None:
    """Buffer one JSONL record, flushing if the byte threshold is reached."""
    self._buf.append(line)
    self._buf_bytes += len(line) + 1
    if self._buf_bytes >= self._flush_bytes:
        await self.flush()
```

### tick

```python
tick()
```

Flush on the interval, or emit a heartbeat when idle.

Source code in `src/resoluto/sandbox/telemetry.py`

```python
async def tick(self) -> None:
    """Flush on the interval, or emit a heartbeat when idle."""
    now = self._clock()
    if self._buf and (now - self._last_flush) >= self._flush_interval_s:
        await self.flush()
    elif not self._buf and (now - self._last_flush) >= self._heartbeat_s:
        await self.emit_line(self._heartbeat_factory(now))
        await self.flush()
```

### close

```python
close()
```

Final flush plus a manifest naming the highest chunk index.

Source code in `src/resoluto/sandbox/telemetry.py`

```python
async def close(self) -> None:
    """Final flush plus a manifest naming the highest chunk index."""
    if self._closed:
        return
    await self.flush()
    manifest = json.dumps({"total_chunks": self._index}).encode("utf-8")
    await self._store.put(f"{self._prefix}/{_MANIFEST}", manifest)
    self._closed = True
```

## resoluto.sandbox.ChunkReader

```python
ChunkReader(
    store,
    prefix,
    *,
    dead_after_s=120.0,
    clock=monotonic,
    progress_filter=None,
)
```

Host-side tail of a run's chunk objects in contiguous index order, with a silence-based death signal.

Source code in `src/resoluto/sandbox/telemetry.py`

```python
def __init__(
    self,
    store: Conduit,
    prefix: str,
    *,
    dead_after_s: float = 120.0,
    clock: Callable[[], float] = time.monotonic,
    progress_filter: Callable[[str], bool] | None = None,
) -> None:
    self._store = store
    self._prefix = prefix.rstrip("/")
    self._dead_after_s = dead_after_s
    self._clock = clock
    self._progress_filter = progress_filter
    self._seen = 0
    self._last_progress = clock()
    self._last_arrival = clock()
    self._total: int | None = None
    self._armed = False
```

### armed

```python
armed
```

Whether the sandbox ever reached RUNNING (the silence window only arms there).

### substrate_silent

```python
substrate_silent
```

True when no chunk has arrived within the death window; false until armed.

### poll_lines

```python
poll_lines()
```

Return newly-available JSONL records in contiguous index order, updating liveness.

Source code in `src/resoluto/sandbox/telemetry.py`

```python
async def poll_lines(self) -> list[str]:
    """Return newly-available JSONL records in contiguous index order, updating liveness."""
    infos = await self._store.list_prefix(self._prefix)
    present: set[int] = set()
    for info in infos:
        m = _CHUNK_RE.search(info.key)
        if m:
            present.add(int(m.group(1)))
        elif info.key.endswith(_MANIFEST) and self._total is None:
            self._total = json.loads(await self._store.get(info.key)).get("total_chunks")

    lines: list[str] = []
    nxt = self._seen + 1
    while nxt in present:
        raw = await self._store.get(_chunk_key(self._prefix, nxt))
        lines.extend(line for line in raw.decode("utf-8").splitlines() if line.strip())
        self._seen = nxt
        nxt += 1

    if lines:
        self._last_arrival = self._clock()
        if self._progress_filter is None:
            self._last_progress = self._clock()
        else:
            verdicts = [self._progress_filter(line) for line in lines]
            if any(verdicts):
                self._last_progress = self._clock()
    return lines
```

### arm

```python
arm()
```

Start the silence window, idempotently; until armed both death signals stay false.

Source code in `src/resoluto/sandbox/telemetry.py`

```python
def arm(self) -> None:
    """Start the silence window, idempotently; until armed both death signals stay false."""
    if self._armed:
        return
    self._armed = True
    self._last_progress = self._last_arrival = self._clock()
```

### is_dead

```python
is_dead()
```

True when no progress line has arrived within the death window; false until armed or finished.

Source code in `src/resoluto/sandbox/telemetry.py`

```python
def is_dead(self) -> bool:
    """True when no progress line has arrived within the death window; false until armed or finished."""
    if not self._armed or self.finished:
        return False
    return (self._clock() - self._last_progress) > self._dead_after_s
```

## resoluto.sandbox.SpanEmitter

```python
SpanEmitter(shipper, run_id, *, clock=time)
```

Source code in `src/resoluto/sandbox/spans.py`

```python
def __init__(
    self, shipper: ChunkShipper, run_id: str, *, clock: Callable[[], float] = time.time
) -> None:
    self._ship = shipper
    self._run_id = run_id
    self._clock = clock
```

## resoluto.sandbox.SpanEvent

Bases: `BaseModel`

One observability record on the JSONL wire: a span open/close or a log line.
