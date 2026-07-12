# Staging

The input/output plumbing between the host workspace and the conduit: stage a workspace in before a run, collect and fetch declared output globs back out after it.

## resoluto.sandbox.stage_inputs

```python
stage_inputs(store, prefix, workspace_dir)
```

Extract every input archive under `inbox/` into the workspace; returns the keys staged.

Source code in `src/resoluto/sandbox/staging.py`

```python
async def stage_inputs(store: Conduit, prefix: str, workspace_dir: str) -> list[str]:
    """Extract every input archive under `inbox/` into the workspace; returns the keys staged."""
    dest = Path(workspace_dir)
    staged: list[str] = []
    for info in await store.list_prefix(f"{prefix.rstrip('/')}/{INBOX}"):
        if info.key.endswith(_ARCHIVE_SUFFIXES):
            _extract(await store.get(info.key), dest)
            staged.append(info.key)
    return staged
```

## resoluto.sandbox.put_dir

```python
put_dir(
    store,
    prefix,
    local_dir,
    *,
    name="workspace",
    exclude=_DEFAULT_EXCLUDES,
    protect=frozenset(),
    paths=None,
)
```

Tar `local_dir` and put it under `inbox/`; returns the object key.

`paths` (each relative to `local_dir`) scopes the archive to just those subtrees. Pass the caller's paths so a run only ever stages the inputs it uses — never the surrounding workspace (deps, sibling repos, or the object store itself). `None` = the whole dir.

Source code in `src/resoluto/sandbox/staging.py`

```python
async def put_dir(
    store: Conduit,
    prefix: str,
    local_dir: str,
    *,
    name: str = "workspace",
    exclude: frozenset[str] = _DEFAULT_EXCLUDES,
    protect: frozenset[str] = frozenset(),
    paths: list[str] | None = None,
) -> str:
    """Tar `local_dir` and put it under `inbox/`; returns the object key.

    `paths` (each relative to `local_dir`) scopes the archive to just those subtrees. Pass the
    caller's paths so a run only ever stages the inputs it uses — never the surrounding
    workspace (deps, sibling repos, or the object store itself). `None` = the whole dir."""
    key = f"{prefix.rstrip('/')}/{INBOX}/{name}.tar.gz"
    await store.put(key, _archive(Path(local_dir), paths, exclude, protect))
    return key
```

## resoluto.sandbox.collect_outputs

```python
collect_outputs(
    store, prefix, workspace_dir, paths, *, name="output"
)
```

Tar the declared output `paths` and put them under `outbox/`; returns the object key.

Source code in `src/resoluto/sandbox/staging.py`

```python
async def collect_outputs(
    store: Conduit, prefix: str, workspace_dir: str, paths: list[str], *, name: str = "output"
) -> str:
    """Tar the declared output `paths` and put them under `outbox/`; returns the object key."""
    key = f"{prefix.rstrip('/')}/{OUTBOX}/{name}.tar.gz"
    await store.put(key, _archive(Path(workspace_dir), paths))
    return key
```

## resoluto.sandbox.fetch_outputs

```python
fetch_outputs(store, prefix, dest_dir, allowed_paths=None)
```

Extract every output archive under `outbox/` into `dest_dir`; returns the keys fetched.

`allowed_paths` (the caller's declared `output_paths`) scopes what is materialized: only members matching those globs land in `dest_dir`, so an untrusted guest cannot smuggle undeclared files into the caller's workspace. `None` extracts everything (low-level/legacy use).

Source code in `src/resoluto/sandbox/staging.py`

```python
async def fetch_outputs(
    store: Conduit, prefix: str, dest_dir: str, allowed_paths: list[str] | None = None
) -> list[str]:
    """Extract every output archive under `outbox/` into `dest_dir`; returns the keys fetched.

    `allowed_paths` (the caller's declared `output_paths`) scopes what is materialized: only members
    matching those globs land in `dest_dir`, so an untrusted guest cannot smuggle undeclared files
    into the caller's workspace. `None` extracts everything (low-level/legacy use)."""
    dest = Path(dest_dir)
    fetched: list[str] = []
    for info in await store.list_prefix(f"{prefix.rstrip('/')}/{OUTBOX}"):
        if info.key.endswith(_ARCHIVE_SUFFIXES):
            data = await store.get(info.key)
            if allowed_paths is None:
                _extract(data, dest)
            else:
                _extract_declared(data, dest, allowed_paths)
            fetched.append(info.key)
    return fetched
```
