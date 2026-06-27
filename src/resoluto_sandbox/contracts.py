"""Pure contracts for the store-mediated sandbox — pydantic + ABCs, no platform deps.

The whole system hangs off three interfaces:
  - `SandboxRuntime` — the ONE platform-specific surface (k8s / ECS / Fly / docker).
  - `Conduit`        — durable rendezvous (localfs / S3-minio / GCS).
  - `SandboxPool`    — platform-independent admission (see pool.py).

Comms is store-mediated: a passive sandbox self-reports append-only JSONL into its
object-store prefix; the orchestrator launches, tails the store, reaps. No
in-sandbox server, no long-lived stream — the long-lived-stream wedge cannot exist here.
"""
from __future__ import annotations

import logging
import os
import re
from abc import ABC, abstractmethod
from contextlib import AbstractAsyncContextManager
from typing import Literal, Protocol, runtime_checkable

from pydantic import BaseModel, Field

# ONE quantity parser (IEC/SI binary+decimal suffixes → bytes), used ONLY at the
# orchestrator boundary to turn human-written knobs ('4Gi') into the neutral byte counts
# carried by `Resources`. A runtime NEVER parses — it renders the neutral ints to its own
# platform. Lives here (dep-light contracts) so pool + worker share exactly one parser.
_QUANTITY_FACTORS: dict[str, int] = {
    "Ki": 1024, "Mi": 1024**2, "Gi": 1024**3, "Ti": 1024**4, "Pi": 1024**5,
    "K": 1000, "M": 1000**2, "G": 1000**3, "T": 1000**4, "P": 1000**5,
}
_QUANTITY_RE = re.compile(r"^(\d+)(Ki|Mi|Gi|Ti|Pi|K|M|G|T|P)?$")


def parse_quantity(s: str) -> int:
    """Parse a binary/decimal byte quantity ('4Gi', '512Mi', '536870912') to bytes.

    Fail-loud on garbage (anchored regex), so a malformed budget/limit is caught at the
    source rather than silently mis-parsed."""
    m = _QUANTITY_RE.match(s.strip())
    if not m:
        raise ValueError(f"Cannot parse byte quantity: {s!r}")
    return int(m.group(1)) * _QUANTITY_FACTORS.get(m.group(2) or "", 1)


class Resources(BaseModel):
    """Platform-NEUTRAL resource request for one sandbox — raw units only, zero platform
    notation. Each `SandboxRuntime` renders these to its own platform (k8s quantity strings,
    docker `--memory/--cpus` flags); no runtime ever sees, or translates, another's vocabulary.
    Build from human knobs ONCE via `from_quantities` at the orchestrator boundary."""

    memory_bytes: int
    cpu_cores: float
    disk_bytes: int | None = None        # ephemeral storage; None → runtime default
    dind_graph_bytes: int | None = None  # inner-docker graph budget; None → not a dind step

    @classmethod
    def from_quantities(
        cls, *, memory: str, cpu: str = "2", disk: str | None = None, dind_graph: str | None = None,
    ) -> "Resources":
        """Build from human quantity strings (operator knobs, e.g. '4Gi', '2'). Parsing happens
        HERE, at the boundary; only neutral ints flow onward to the runtimes."""
        return cls(
            memory_bytes=parse_quantity(memory),
            cpu_cores=float(cpu),
            disk_bytes=parse_quantity(disk) if disk else None,
            dind_graph_bytes=parse_quantity(dind_graph) if dind_graph else None,
        )

_logger = logging.getLogger(__name__)


def check_runtime_class_guard(runtime_class: str) -> None:
    """Refuse non-Kata runtime classes unless RESOLUTO_TRUSTED_LOCAL is set.

    Args: runtime_class — the K8s runtime's own configured runtime class (k8s-private; not
    part of the neutral SandboxLaunchSpec).
    Raises RuntimeError when runtime_class is not 'kata' and trusted-local flag absent.
    """
    if runtime_class.strip().lower() == "kata":
        return
    if "RESOLUTO_TRUSTED_LOCAL" not in os.environ:
        raise RuntimeError(
            f"Isolation downgrade refused: runtime_class={runtime_class!r}. "
            "Set RESOLUTO_TRUSTED_LOCAL to permit non-Kata runtimes."
        )
    _logger.warning("[sandbox-guard] trusted-local: non-Kata runtime_class=%r permitted", runtime_class)

# ── launch / handle / status ────────────────────────────────────────────────


class SandboxLaunchSpec(BaseModel):
    """What the orchestrator hands a runtime to launch ONE sandbox — PLATFORM-NEUTRAL.

    Resources are neutral (`Resources`, raw bytes/cores); each runtime renders them to its
    own platform. The neutral spec carries NO platform vocabulary — k8s-only concerns
    (runtimeClass/Kata, the tmpfs-vs-virtio-blk graph backend) are the K8s runtime's private
    config, not fields here.

    `flavor` is the neutral isolation tier (tier-0/1 → plain; tier-2 + a live dev env → dind).
    `privileged` is required only by `dind` (inner dockerd); GUEST-SCOPED under Kata.
    """

    image: str
    flavor: Literal["dind", "plain"] = "plain"
    env: dict[str, str] = Field(default_factory=dict)
    command: list[str] | None = None
    args: list[str] | None = None
    # Neutral resource request. EVERY production producer sets this explicitly — the lane path via
    # build_launch_spec(profile) and the one-shot SubstrateBackend via its own explicit Resources — so
    # nothing relies on this default at runtime; it exists only as a value-object construction
    # convenience (tests building bare specs for non-resource assertions). Not a lane fallback.
    resources: Resources = Field(default_factory=lambda: Resources(memory_bytes=4 * 1024**3, cpu_cores=2.0))
    privileged: bool = False
    labels: dict[str, str] = Field(default_factory=dict)
    # Opaque pod metadata the substrate stamps VERBATIM and never interprets — the seam an
    # EXTERNAL admission layer (e.g. Kueue) composes through, with zero coupling: the caller
    # sets `labels["kueue.x-k8s.io/queue-name"]` + a scheduling gate; the substrate has no
    # idea what they mean. Empty (the default) → normal scheduling, no external admitter.
    annotations: dict[str, str] = Field(default_factory=dict)
    scheduling_gates: list[str] = Field(default_factory=list)  # k8s pod schedulingGates (opaque names)
    store_prefix: str  # run/<run_id>/nodes/<node_id> — where the sandbox self-reports
    store_write_token: str = ""  # prefix-scoped, write-only, expiring
    deadline_seconds: int | None = None  # optional pod cap; None = no wall-clock deadline


class SandboxHandle(BaseModel):
    id: str  # runtime-native, e.g. "<namespace>/<pod>"
    labels: dict[str, str] = Field(default_factory=dict)


class SandboxStatus(BaseModel):
    phase: Literal["pending", "running", "succeeded", "failed", "unknown"]
    reason: str = ""
    exit_code: int | None = None

    @property
    def terminal(self) -> bool:
        return self.phase in ("succeeded", "failed")


class NodeResult(BaseModel):
    """The lane's typed work product — written by the in-sandbox runner to
    `<prefix>/result.json`, read back by the orchestrator. Generic by design: it
    carries NO gate/lane/git vocabulary (that mapping is the worker's, upstream).

    The first block is the sandbox's self-report; the `observed_*` / `reason` /
    `substrate_logs` block is filled by the ORCHESTRATOR from out-of-guest signals
    (the in-guest verdict is work product, not a trust decision).
    """

    node_id: str = ""
    status: Literal["success", "failure"] = "failure"
    exit_code: int | None = None
    output_archive: str | None = None
    observed_phase: str = ""
    reason: str = ""
    substrate_logs: str = ""


# ── object store ────────────────────────────────────────────────────────────


class ObjectInfo(BaseModel):
    key: str
    size: int


class ConduitError(Exception):
    """A transport/I/O failure talking to the conduit (disk/storage full,
    connection refused, timeout). Substrate-native: the worker layer translates
    this into the pipeline's fatal InfrastructureError — the sandbox package has
    no dependency on resoluto-core."""


class Conduit(ABC):
    """Durable key/value rendezvous. Backends: localfs, S3 (minio), GCS.

    The reader uses `list_prefix` + whole-object `get` to tail append-only chunk
    objects (telemetry.py). No append semantics needed — chunks are immutable.
    """

    @abstractmethod
    async def put(self, key: str, data: bytes) -> None: ...

    @abstractmethod
    async def get(self, key: str) -> bytes: ...

    @abstractmethod
    async def list_prefix(self, prefix: str) -> list[ObjectInfo]: ...

    async def copy_prefix(self, src_prefix: str, dst_prefix: str) -> int:
        """Copy every object under src_prefix to dst_prefix (suffix-preserving),
        returning the count copied. No-ops cleanly when src has no objects. The
        default round-trips bytes through get/put; backends that support
        server-side copy override this to avoid the host round-trip."""
        src, dst = src_prefix.rstrip("/"), dst_prefix.rstrip("/")
        objs = await self.list_prefix(src)
        for o in objs:
            rel = o.key[len(src):].lstrip("/")
            await self.put(f"{dst}/{rel}", await self.get(o.key))
        return len(objs)


# ── runtime ─────────────────────────────────────────────────────────────────


class SandboxRuntime(ABC):
    """The ONE platform-specific surface. The pool owns admission/ordering; the
    runtime owns placement (k8s schedules the Pod, ECS places the task, …).
    """

    @abstractmethod
    async def launch(self, spec: SandboxLaunchSpec) -> SandboxHandle: ...

    @abstractmethod
    async def status(self, handle: SandboxHandle) -> SandboxStatus: ...

    @abstractmethod
    async def destroy(self, handle: SandboxHandle) -> None: ...

    @abstractmethod
    async def sweep(self, labels: dict[str, str]) -> int:
        """Destroy every sandbox whose labels include all given pairs — the leak
        backstop for handles lost on orchestrator death. Returns count destroyed."""

    async def logs(self, handle: SandboxHandle, *, tail: int = 200) -> str:
        """Substrate-side forensics (pod terminated reason / stdout). FORENSIC
        ONLY — the main channel is the object store. Untrusted on read."""
        raise NotImplementedError


# ── admission (the swappable WHEN) ──────────────────────────────────────────


@runtime_checkable
class Lease(Protocol):
    """An acquired sandbox slot, an async context manager exposing the live handle.
    Closing it releases the slot (and, for the in-process pool, reaps the pod)."""

    handle: SandboxHandle

    async def __aenter__(self) -> "Lease": ...
    async def __aexit__(self, *exc: object) -> None: ...


@runtime_checkable
class Admission(Protocol):
    """The swappable WHETHER/WHEN-to-launch concern, SEPARATE from the substrate (the
    HOW). `acquire(spec)` decides if/when a launch is allowed, then launches and returns
    a Lease. Implementations: the in-process `SandboxPool` (local/non-cluster path); a
    no-op identity admitter (launch immediately); or NONE at all when an EXTERNAL
    admission layer (k8s scheduler, Kueue) already gated the pod via its metadata. The
    substrate (`SandboxRuntime`) never imports or depends on any admitter — the only thing
    that connects them is the `SandboxLaunchSpec`'s opaque pod metadata."""

    async def acquire(self, spec: SandboxLaunchSpec) -> AbstractAsyncContextManager[Lease]: ...


# ── observability span event ─────────────────────────────────────────────────


class SpanEvent(BaseModel):
    """One observability record on the JSONL wire — a span open/close or a log
    line — carrying the tree (`span_id`/`parent_span_id`), inputs/outputs, or a
    redacted log payload. The same schema drives live UI, replay, and forensics.
    """

    run_id: str
    span_id: str
    parent_span_id: str = ""
    kind: str  # run | phase | node | lane | attempt | gate | agent | tool | log
    name: str = ""
    event: Literal["open", "close", "log"]
    ts: float  # epoch seconds (stamped by the emitter)
    status: str = ""  # close: success | failure | …
    data: dict = Field(default_factory=dict)  # inputs / outputs / log payload — REDACTED
