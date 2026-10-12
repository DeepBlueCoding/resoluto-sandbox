# Sandbox

The single public entrypoint and the outcome it returns.

## resoluto.sandbox.Sandbox

```python
Sandbox(*, backend='local', image=None)
```

Run a program in a sandbox. Holds a Backend (selected by name or injected).

Source code in `src/resoluto/sandbox/client.py`

```python
def __init__(self, *, backend: "Backend | str" = "local", image: str | None = None) -> None:
    if isinstance(backend, Backend):
        self._backend = backend
    elif backend == "local":
        self._backend = _build_local_backend(image)
    elif backend == "k8s":
        self._backend = _build_k8s_backend(image)
    else:
        raise ValueError(f"unknown backend {backend!r} (expected 'local', 'k8s', or a Backend)")
```

### run

```python
run(
    argv,
    *,
    workspace=None,
    stdin=None,
    env=None,
    env_file=None,
    secrets=None,
    output_paths=None,
    stream=None,
    egress=None,
)
```

Run `argv` in the sandbox with `workspace` cwd, `env` overlay, `output_paths` globs collected into `RunResult.artifacts`, and live output to `stream`; returns a `RunResult`.

`env_file` parses a dotenv-format file host-side and merges it under `env` (`env` wins on conflict) — a convenience for literal config, NOT a security mechanism: values still land as literal env entries, same as `env`.

`secrets` maps an env var name to either a `SecretKeyRef` (k8s-native — references an existing Kubernetes Secret's key via `valueFrom.secretKeyRef`, zero guest-side code; ignored on the `local` backend) or a plain `str` (a provider-specific ref resolved GUEST-SIDE by the configured `SecretProvider` — see `secrets.py` — so the plaintext value never touches the host, the pod spec, or any log).

`egress` is THIS run's allowed-domain list (e.g. `["api.anthropic.com"]`) — per-step networking set up on the fly and torn down after, with no re-provisioning. `None`/`[]` = deny all outbound (secure default). Currently applied by the `local` backend's SNI proxy.

Source code in `src/resoluto/sandbox/client.py`

```python
def run(
    self,
    argv: Sequence[str],
    *,
    workspace: str | None = None,
    stdin: str | bytes | None = None,
    env: dict[str, str] | None = None,
    env_file: str | None = None,
    secrets: "dict[str, str | SecretKeyRef] | None" = None,
    output_paths: Sequence[str] | None = None,
    stream: IO[str] | None = None,
    egress: Sequence[str] | None = None,
) -> RunResult:
    """Run ``argv`` in the sandbox with ``workspace`` cwd, ``env`` overlay, ``output_paths`` globs
    collected into ``RunResult.artifacts``, and live output to ``stream``; returns a ``RunResult``.

    ``env_file`` parses a dotenv-format file host-side and merges it under ``env`` (``env`` wins
    on conflict) — a convenience for literal config, NOT a security mechanism: values still land
    as literal env entries, same as ``env``.

    ``secrets`` maps an env var name to either a ``SecretKeyRef`` (k8s-native — references an
    existing Kubernetes Secret's key via ``valueFrom.secretKeyRef``, zero guest-side code; ignored
    on the ``local`` backend) or a plain ``str`` (a provider-specific ref resolved GUEST-SIDE by
    the configured ``SecretProvider`` — see ``secrets.py`` — so the plaintext value never touches
    the host, the pod spec, or any log).

    ``egress`` is THIS run's allowed-domain list (e.g. ``["api.anthropic.com"]``) — per-step
    networking set up on the fly and torn down after, with no re-provisioning. ``None``/``[]`` =
    deny all outbound (secure default). Currently applied by the ``local`` backend's SNI proxy.
    """
    return self._backend.run(
        argv,
        workspace=workspace,
        stdin=stdin,
        env=env,
        env_file=env_file,
        secrets=secrets,
        output_paths=output_paths,
        stream=stream,
        egress=egress,
    )
```

## resoluto.sandbox.RunResult

Bases: `BaseModel`

Outcome of one `run()`: exit code, output/errors, collected `artifacts` paths, parsed `result`, and a substrate `reason`.
