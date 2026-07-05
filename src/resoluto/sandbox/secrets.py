"""Secrets seam: a guest-side SecretProvider (ABC) for stores the guest fetches from itself
(mirrors Conduit + conduit/factory.py's store_from_env), plus SecretKeyRef, the k8s-native marker
for referencing an existing Kubernetes Secret object (rendered via valueFrom.secretKeyRef, zero
guest-side code). No concrete SecretProvider ships yet — this defines the seam so one (Vault, AWS
Secrets Manager, GCP Secret Manager, ...) can be added later without touching any other module."""
from __future__ import annotations

import os
from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass(frozen=True)
class SecretKeyRef:
    """Reference to an existing Kubernetes Secret's key. k8s-only: rendered as
    valueFrom.secretKeyRef by K8sSandboxRuntime; ignored by the local backend (KataNerdctlSandboxRuntime
    never reads SandboxLaunchSpec.k8s_secret_refs). The Secret itself must already exist — created by
    kubectl, External Secrets Operator, or any other means; resoluto-sandbox never creates or syncs one."""

    name: str
    key: str


class SecretProvider(ABC):
    """Guest-side resolver: turns a declarative, provider-specific reference string into its
    plaintext value. Runs INSIDE the sandbox (see runner_main.py), never on the host — the host only
    ever holds an already-scoped credential (via RESOLUTO_SECRETS_* env), never the secret itself.
    Mirrors Conduit: an opaque string ref, same as Conduit.get(key: str)."""

    @abstractmethod
    async def get(self, ref: str) -> str: ...


def secrets_from_env(env: dict[str, str] | None = None) -> SecretProvider | None:
    """Build a SecretProvider from environment variables. Inputs: optional env dict (defaults to
    os.environ). Output: None if RESOLUTO_SECRETS_KIND is unset (no secrets declared for this run);
    raises for any kind, since no concrete SecretProvider ships yet — add one below and dispatch it
    here when you have a store to implement against."""
    env = env if env is not None else os.environ
    kind = env.get("RESOLUTO_SECRETS_KIND")
    if kind is None:
        return None
    raise RuntimeError(
        f"RESOLUTO_SECRETS_KIND={kind!r} but no SecretProvider implementation ships yet — "
        "implement a SecretProvider subclass and dispatch it here in secrets_from_env()."
    )
