# Conduit

The durable key/value rendezvous between host and sandbox — the only channel between the two halves. `store_from_env` selects a concrete backend from the environment; every backend implements the same three-operation `Conduit` interface. `ObjectInfo` describes a listed object.

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
        rel = o.key[len(src) :].lstrip("/")
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

## resoluto.sandbox.ObjectInfo

Bases: `BaseModel`

## resoluto.sandbox.conduit.factory.store_from_env

```python
store_from_env(env=None)
```

Build a Conduit from environment variables. Inputs: optional env dict (defaults to os.environ). Output: a concrete Conduit for the requested RESOLUTO_STORE_KIND.

Source code in `src/resoluto/sandbox/conduit/factory.py`

```python
def store_from_env(env: dict[str, str] | None = None) -> Conduit:
    """Build a Conduit from environment variables. Inputs: optional env dict (defaults
    to os.environ). Output: a concrete Conduit for the requested RESOLUTO_STORE_KIND."""
    env = env if env is not None else os.environ
    kind = env["RESOLUTO_STORE_KIND"]
    if kind == "stdout":
        from resoluto.sandbox.conduit.stdout import StdoutConduit

        return StdoutConduit()
    if kind == "localfs":
        from resoluto.sandbox.conduit import LocalConduit

        return LocalConduit(env["RESOLUTO_STORE_ROOT"])
    if kind == "s3":
        from resoluto.sandbox.conduit.s3 import S3Conduit

        write_token = env.get("RESOLUTO_STORE_WRITE_TOKEN")
        if write_token:
            tok = json.loads(write_token)
            return S3Conduit(
                tok["bucket"],
                endpoint_url=tok.get("endpoint_url"),
                region_name=tok.get("region", "us-east-1"),
                aws_access_key_id=tok["access_key_id"],
                aws_secret_access_key=tok["secret_access_key"],
                aws_session_token=tok.get("session_token"),
            )
        return S3Conduit(
            env["RESOLUTO_STORE_BUCKET"],
            endpoint_url=env.get("RESOLUTO_STORE_ENDPOINT") or None,
            region_name=env.get("RESOLUTO_STORE_REGION", "us-east-1"),
            aws_access_key_id=env.get("AWS_ACCESS_KEY_ID"),
            aws_secret_access_key=env.get("AWS_SECRET_ACCESS_KEY"),
        )
    if kind == "gcs":
        from resoluto.sandbox.conduit.gcs import GcsConduit

        if env.get("RESOLUTO_STORE_WRITE_TOKEN"):
            raise RuntimeError(
                "RESOLUTO_STORE_KIND=gcs cannot honor a prefix-scoped RESOLUTO_STORE_WRITE_TOKEN "
                "(that is the s3 STS path) — GcsConduit is a single-tenant host-side store. "
                "Refusing rather than silently granting whole-service-account access."
            )
        return GcsConduit(
            env["RESOLUTO_STORE_BUCKET"],
            service_file=env.get("RESOLUTO_GCS_SERVICE_FILE"),
        )
    raise RuntimeError(f"unknown RESOLUTO_STORE_KIND={kind!r}")
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

## resoluto.sandbox.conduit.s3.S3Conduit

```python
S3Conduit(
    bucket,
    *,
    endpoint_url=None,
    region_name=None,
    aws_access_key_id=None,
    aws_secret_access_key=None,
    aws_session_token=None,
)
```

Bases: `Conduit`

Source code in `src/resoluto/sandbox/conduit/s3.py`

```python
def __init__(
    self,
    bucket: str,
    *,
    endpoint_url: str | None = None,
    region_name: str | None = None,
    aws_access_key_id: str | None = None,
    aws_secret_access_key: str | None = None,
    aws_session_token: str | None = None,
) -> None:
    self._bucket = bucket
    self._client_kwargs = {
        "endpoint_url": endpoint_url,
        "region_name": region_name,
        "aws_access_key_id": aws_access_key_id,
        "aws_secret_access_key": aws_secret_access_key,
        "aws_session_token": aws_session_token,
    }
    self._session = None
```

### aclose

```python
aclose()
```

Drop the cached session so a stale one isn't reused after teardown.

Source code in `src/resoluto/sandbox/conduit/s3.py`

```python
async def aclose(self) -> None:
    """Drop the cached session so a stale one isn't reused after teardown."""
    self._session = None
```

### ensure_bucket

```python
ensure_bucket()
```

Create the bucket if absent.

Source code in `src/resoluto/sandbox/conduit/s3.py`

```python
async def ensure_bucket(self) -> None:
    """Create the bucket if absent."""
    from botocore.exceptions import ClientError

    async with self._client() as c:
        try:
            await c.head_bucket(Bucket=self._bucket)
        except ClientError:
            await c.create_bucket(Bucket=self._bucket)
```

## resoluto.sandbox.conduit.gcs.GcsConduit

```python
GcsConduit(bucket, *, service_file=None)
```

Bases: `Conduit`

Source code in `src/resoluto/sandbox/conduit/gcs.py`

```python
def __init__(self, bucket: str, *, service_file: str | None = None) -> None:
    self._bucket = bucket
    self._service_file = service_file
    self._storage = None
```
