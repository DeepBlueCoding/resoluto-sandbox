"""Pure contracts for the store-mediated sandbox — pydantic + ABCs, no platform deps.

The whole system hangs off three interfaces (design §11.1):
  - `SandboxRuntime` — the ONE platform-specific surface (k8s / ECS / Fly / docker).
  - `ObjectStore`    — durable rendezvous (localfs / S3-minio / GCS).
  - `SandboxPool`    — platform-independent admission (see pool.py).

Comms is store-mediated: a passive sandbox self-reports append-only JSONL into its
object-store prefix; the orchestrator launches, tails the store, reaps. No
in-sandbox server, no long-lived stream — the RES-236 wedge cannot exist here.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Literal

from pydantic import BaseModel, Field

# ── launch / handle / status ────────────────────────────────────────────────


class SandboxLaunchSpec(BaseModel):
    """What the orchestrator hands a runtime to launch ONE sandbox.

    `flavor` maps to the §4.1 tier × dev_environment.kind:
      tier-0/tier-1 → plain;  tier-2 + docker_compose → dind;  tier-2 + none → plain.
    `privileged` is GUEST-SCOPED under Kata (privileged_without_host_devices) — the
    host pod stays unprivileged. Required only by `dind` lanes (inner dockerd).
    """

    image: str
    flavor: Literal["dind", "plain"] = "plain"
    runtime_class: str = "kata"  # k8s runtimeClass; "" / "runc" only for trusted-local
    env: dict[str, str] = Field(default_factory=dict)
    command: list[str] | None = None
    args: list[str] | None = None
    cpu: str = "2"
    memory: str = "4Gi"
    ephemeral_storage: str = "8Gi"
    privileged: bool = False
    labels: dict[str, str] = Field(default_factory=dict)
    store_prefix: str  # run/<run_id>/nodes/<node_id> — where the sandbox self-reports
    store_write_token: str = ""  # prefix-scoped, write-only, expiring (§12.3)
    deadline_seconds: int = 1800  # SUBSTRATE cap (never agent-work, §5.2)


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
    (§12.12 — the in-guest verdict is work product, not a trust decision).
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


class ObjectStore(ABC):
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
        ONLY — the main channel is the object store. Untrusted on read (§12.12)."""
        raise NotImplementedError


# ── observability span event (§13) ──────────────────────────────────────────


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
