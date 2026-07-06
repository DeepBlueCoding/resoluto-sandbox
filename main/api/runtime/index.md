# Runtime & Contracts

The isolation/placement seam (`SandboxRuntime`), the orchestration backend that drives the stage → run → collect flow, and the platform-neutral launch spec.

## resoluto.sandbox.SandboxRuntime

Bases: `ABC`

The platform-specific surface that launches, polls, and destroys a sandbox.

### sweep

```python
sweep(labels)
```

Destroy every sandbox whose labels include all given pairs; return count destroyed.

Source code in `src/resoluto/sandbox/contracts.py`

```python
@abstractmethod
async def sweep(self, labels: dict[str, str]) -> int:
    """Destroy every sandbox whose labels include all given pairs; return count destroyed."""
```

### logs

```python
logs(handle, *, tail=200)
```

Return tail lines of substrate-side logs for forensics.

Source code in `src/resoluto/sandbox/contracts.py`

```python
async def logs(self, handle: SandboxHandle, *, tail: int = 200) -> str:
    """Return tail lines of substrate-side logs for forensics."""
    raise NotImplementedError
```

## resoluto.sandbox.SubstrateBackend

```python
SubstrateBackend(
    *,
    runtime,
    conduit,
    image,
    store_env,
    resources=None,
    dead_after_s=600.0,
)
```

Bases: `Backend`

Runs a program in a sandbox via an injected runtime, conduit, image, and store env.

Source code in `src/resoluto/sandbox/backends/substrate.py`

```python
def __init__(
    self,
    *,
    runtime: SandboxRuntime,
    conduit: Conduit,
    image: str,
    store_env: dict[str, str],
    resources: Resources | None = None,
    dead_after_s: float = 600.0,
) -> None:
    if not image:
        raise ValueError("SubstrateBackend requires image=...")
    self._runtime = runtime
    self._conduit = conduit
    self._image = image
    self._store_env = store_env
    self._resources = resources or Resources.from_quantities(memory="4Gi", cpu="2")
    self._dead_after_s = dead_after_s
```

## resoluto.sandbox.SandboxLaunchSpec

Bases: `BaseModel`

Platform-neutral spec the orchestrator hands a runtime to launch one sandbox.
