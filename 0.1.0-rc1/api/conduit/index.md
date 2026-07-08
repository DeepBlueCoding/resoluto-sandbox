# Conduit

The durable key/value rendezvous between host and sandbox — the only channel between the two halves.

## resoluto.sandbox.Conduit

Bases: `ABC`

Durable key/value rendezvous (localfs, S3, GCS).

### copy_prefix

```python
copy_prefix(src_prefix, dst_prefix)
```

Copy every object under src_prefix to dst_prefix, returning the count copied.

Source code in `src/resoluto/sandbox/contracts.py`

```python
async def copy_prefix(self, src_prefix: str, dst_prefix: str) -> int:
    """Copy every object under src_prefix to dst_prefix, returning the count copied."""
    src, dst = src_prefix.rstrip("/"), dst_prefix.rstrip("/")
    objs = await self.list_prefix(src)
    for o in objs:
        rel = o.key[len(src):].lstrip("/")
        await self.put(f"{dst}/{rel}", await self.get(o.key))
    return len(objs)
```

### aclose

```python
aclose()
```

Release any cached client/session. Default no-op; override where there's something to release (a cached HTTP session, connection pool, etc). One name across every Conduit.

Source code in `src/resoluto/sandbox/contracts.py`

```python
async def aclose(self) -> None:
    """Release any cached client/session. Default no-op; override where there's something to
    release (a cached HTTP session, connection pool, etc). One name across every Conduit."""
```

## resoluto.sandbox.LocalConduit

```python
LocalConduit(root, *, world_writable=False)
```

Bases: `Conduit`

Source code in `src/resoluto/sandbox/conduit/local.py`

```python
def __init__(self, root: str | Path, *, world_writable: bool = False) -> None:
    self._root = Path(root)
    self._world_writable = world_writable
    self._root.mkdir(parents=True, exist_ok=True)
    if world_writable:
        self._chmod_world(self._root)
```

## resoluto.sandbox.StdoutConduit

```python
StdoutConduit(*, sink=None)
```

Bases: `Conduit`

Source code in `src/resoluto/sandbox/conduit/stdout.py`

```python
def __init__(self, *, sink: IO[str] | None = None) -> None:
    self._sink = sink if sink is not None else sys.stdout
```
