"""Pydantic + ABC contracts for the store-mediated sandbox."""
from __future__ import annotations

import re
from abc import ABC, abstractmethod
from contextlib import AbstractAsyncContextManager
from typing import Literal, Protocol, runtime_checkable

from pydantic import BaseModel, Field

_QUANTITY_FACTORS: dict[str, int] = {
    "Ki": 1024, "Mi": 1024**2, "Gi": 1024**3, "Ti": 1024**4, "Pi": 1024**5,
    "K": 1000, "M": 1000**2, "G": 1000**3, "T": 1000**4, "P": 1000**5,
}
_QUANTITY_RE = re.compile(r"^(\d+)(Ki|Mi|Gi|Ti|Pi|K|M|G|T|P)?$")


def parse_quantity(s: str) -> int:
    """Parse a binary/decimal byte quantity ('4Gi', '512Mi', '536870912') to bytes."""
    m = _QUANTITY_RE.match(s.strip())
    if not m:
        raise ValueError(f"Cannot parse byte quantity: {s!r}")
    return int(m.group(1)) * _QUANTITY_FACTORS.get(m.group(2) or "", 1)


class Resources(BaseModel):
    """Platform-neutral resource request for one sandbox, in raw bytes/cores."""

    memory_bytes: int
    cpu_cores: float
    disk_bytes: int | None = None
    dind_graph_bytes: int | None = None
    # Where dockerd's image graph lives for a dind step: "tmpfs" (RAM-backed) or "block"
    # (disk-backed volume — image layers stay off RAM). Deliberately a NEUTRAL field: both
    # runtimes (k8s emptyDir, local nerdctl volume) now honor it, so it is no longer a
    # k8s-private concern. Ignored by non-dind steps.
    graph_backend: str = "tmpfs"

    @classmethod
    def from_quantities(
        cls, *, memory: str, cpu: str = "2", disk: str | None = None, dind_graph: str | None = None,
        graph_backend: str = "tmpfs",
    ) -> "Resources":
        """Build a Resources from human quantity strings (e.g. '4Gi', '2')."""
        return cls(
            memory_bytes=parse_quantity(memory),
            cpu_cores=float(cpu),
            disk_bytes=parse_quantity(disk) if disk else None,
            dind_graph_bytes=parse_quantity(dind_graph) if dind_graph else None,
            graph_backend=graph_backend,
        )


def check_runtime_class_guard(runtime_class: str) -> None:
    """Raise RuntimeError unless runtime_class names a Kata runtime."""
    if "kata" in runtime_class.strip().lower():
        return
    raise RuntimeError(
        f"Isolation downgrade refused: runtime_class={runtime_class!r} is not a Kata runtime. "
        "VM-grade isolation is required — there is no trusted-local bypass."
    )


class SandboxLaunchSpec(BaseModel):
    """Platform-neutral spec the caller hands a runtime to launch one sandbox."""

    image: str
    flavor: Literal["dind", "plain"] = "plain"
    env: dict[str, str] = Field(default_factory=dict)
    command: list[str] | None = None
    args: list[str] | None = None
    resources: Resources = Field(default_factory=lambda: Resources(memory_bytes=4 * 1024**3, cpu_cores=2.0))
    privileged: bool = False
    labels: dict[str, str] = Field(default_factory=dict)
    annotations: dict[str, str] = Field(default_factory=dict)
    scheduling_gates: list[str] = Field(default_factory=list)
    store_prefix: str
    store_write_token: str = ""
    # k8s-only: var name -> (Secret name, Secret key), rendered as valueFrom.secretKeyRef by
    # K8sSandboxRuntime. The local backend never reads this field — ignored, not an error.
    k8s_secret_refs: dict[str, tuple[str, str]] = Field(default_factory=dict)
    deadline_seconds: int | None = None
    # Egress POLICY the sandbox applies for THIS step (the graph declares it; the runtime applies
    # it — k8s NetworkPolicy, local SNI proxy). Store connectivity is the runtime's own infra
    # concern and is merged in separately. Empty + public_https=False ⇒ deny-all-but-DNS+store.
    egress_allow: list[str] = Field(default_factory=list)
    egress_public_https: bool = False


class SandboxHandle(BaseModel):
    id: str
    labels: dict[str, str] = Field(default_factory=dict)


class SandboxStatus(BaseModel):
    phase: Literal["pending", "running", "succeeded", "failed", "unknown"]
    reason: str = ""
    exit_code: int | None = None

    @property
    def terminal(self) -> bool:
        return self.phase in ("succeeded", "failed")


class NodeResult(BaseModel):
    """Typed work product the in-sandbox runner writes to `<prefix>/result.json`."""

    node_id: str = ""
    status: Literal["success", "failure"] = "failure"
    exit_code: int | None = None
    output_archive: str | None = None
    observed_phase: str = ""
    reason: str = ""
    substrate_logs: str = ""


class ObjectInfo(BaseModel):
    key: str
    size: int


class ConduitError(Exception):
    """A transport/I/O failure talking to the conduit."""


class Conduit(ABC):
    """Durable key/value rendezvous (localfs, S3, GCS)."""

    @abstractmethod
    async def put(self, key: str, data: bytes) -> None: ...

    @abstractmethod
    async def get(self, key: str) -> bytes: ...

    @abstractmethod
    async def list_prefix(self, prefix: str) -> list[ObjectInfo]: ...

    async def copy_prefix(self, src_prefix: str, dst_prefix: str) -> int:
        """Copy every object under src_prefix to dst_prefix, returning the count copied."""
        src, dst = src_prefix.rstrip("/"), dst_prefix.rstrip("/")
        objs = await self.list_prefix(src)
        for o in objs:
            rel = o.key[len(src):].lstrip("/")
            await self.put(f"{dst}/{rel}", await self.get(o.key))
        return len(objs)

    async def aclose(self) -> None:
        """Release any cached client/session. Default no-op; override where there's something to
        release (a cached HTTP session, connection pool, etc). One name across every Conduit."""


class SandboxRuntime(ABC):
    """The platform-specific surface that launches, polls, and destroys a sandbox."""

    @abstractmethod
    async def launch(self, spec: SandboxLaunchSpec) -> SandboxHandle: ...

    @abstractmethod
    async def status(self, handle: SandboxHandle) -> SandboxStatus: ...

    @abstractmethod
    async def destroy(self, handle: SandboxHandle) -> None: ...

    @abstractmethod
    async def sweep(self, labels: dict[str, str]) -> int:
        """Destroy every sandbox whose labels include all given pairs; return count destroyed."""

    async def logs(self, handle: SandboxHandle, *, tail: int = 200) -> str:
        """Return tail lines of substrate-side logs for forensics."""
        raise NotImplementedError


@runtime_checkable
class Lease(Protocol):
    """An acquired sandbox slot as an async context manager exposing the live handle."""

    handle: SandboxHandle

    async def __aenter__(self) -> "Lease": ...
    async def __aexit__(self, *exc: object) -> None: ...


@runtime_checkable
class Admission(Protocol):
    """Decides whether/when a launch is allowed, then launches and returns a Lease."""

    async def acquire(self, spec: SandboxLaunchSpec) -> AbstractAsyncContextManager[Lease]: ...


class SpanEvent(BaseModel):
    """One observability record on the JSONL wire: a span open/close or a log line."""

    run_id: str
    span_id: str
    parent_span_id: str = ""
    kind: str
    name: str = ""
    event: Literal["open", "close", "log"]
    ts: float
    status: str = ""
    data: dict = Field(default_factory=dict)
