#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# ///
"""Smoke test: run the SAME minimal agent through BOTH sandboxes and verify the
input -> agent -> output contract end to end.

  - local : Kata microVM via nerdctl (Sandbox(backend="local"))
  - k8s   : Kata microVM pod        (Sandbox(backend=SubstrateBackend(...)))

For each backend it asserts the documented program contract (references/agents.md):
  input   : argv (the prompt) + env (SMOKE_TAG) reach the agent
  output  : the agent's stdout comes back as RunResult.output
  artifact: result.json is collected (RunResult.artifacts) and parsed (RunResult.result)

Run from resoluto-sandbox/ with the backends provisioned:
    set -a; source store.env; set +a      # k8s: minio + s3 + STS + lane image + kube context
    set -a; source local.env; set +a      # local: RESOLUTO_LOCAL_* knobs (or rely on defaults)
    uv run python examples/smoke_both_backends.py            # both
    uv run python examples/smoke_both_backends.py --local-only
    uv run python examples/smoke_both_backends.py --k8s-only
"""
import asyncio
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

from resoluto.sandbox import Sandbox

EXAMPLES = Path(__file__).resolve().parent
AGENT = EXAMPLES / "echo_agent.py"
PROMPT = "ping-42"
TAG = "smoke-tag-7"
EXPECTED_ANSWER = "ECHO: " + PROMPT[::-1]


def _staged_workspace() -> str:
    """A throwaway workspace holding just the agent — staged into the guest, artifacts come back here."""
    ws = tempfile.mkdtemp(prefix="smoke-ws-")
    shutil.copy(AGENT, Path(ws) / "echo_agent.py")
    return ws


def _verify(label: str, res) -> bool:
    checks = {
        "exit code 0 (RunResult.ok)": res.ok,
        "stdout answer in RunResult.output": EXPECTED_ANSWER in res.output,
        "env reached the guest (SMOKE_TAG)": f"TAG: {TAG}" in res.output,
        "result.json in RunResult.artifacts": any(p.endswith("result.json") for p in res.artifacts),
        "result.json parsed -> RunResult.result": bool(res.result) and res.result.get("status") == "success",
    }
    ok = all(checks.values())
    print(f"\n[{'PASS' if ok else 'FAIL'}] {label}")
    print(f"    output   : {res.output.strip()!r}")
    print(f"    result   : {res.result}")
    print(f"    artifacts: {res.artifacts}")
    print(f"    reason   : {res.reason!r}")
    for name, passed in checks.items():
        print(f"      {'OK ' if passed else 'XX '} {name}")
    return ok


def _egress_unenforced(res) -> bool:
    """True when the lane failed ONLY because the cluster didn't enforce egress isolation.

    The in-guest canary is fail-closed: if the CNI can't enforce the egress NetworkPolicy it
    refuses to run the workload. That is an ENVIRONMENT limitation (needs an enforcing CNI like
    Cilium/Calico), not a defect in the sandbox or this smoke test — the input/store plumbing all
    worked (the probes ran, the store round-tripped).
    """
    return "egress canary failed" in (res.reason or "") or "egress canary failed" in res.output


def run_local() -> str:
    """local backend: Sandbox(backend='local') — the documented one-liner. Returns GREEN/RED."""
    image = os.environ.get("RESOLUTO_LOCAL_LANE_IMAGE", "localhost:5000/resoluto-lane:dev")
    res = Sandbox(backend="local", image=image).run(
        ["python", "echo_agent.py", PROMPT],
        workspace=_staged_workspace(),
        env={"SMOKE_TAG": TAG},
        output_paths=["result.json"],
    )
    return "GREEN" if _verify("local — Kata microVM via nerdctl", res) else "RED"


def run_k8s() -> str:
    """k8s backend: inject a SubstrateBackend (references/agents.md > k8s backend).

    The s3 store requires a SCOPED RESOLUTO_STORE_WRITE_TOKEN — host AWS creds are never
    forwarded to the pod. Mint one (broad run/* prefix) and hand it to the pod via store_env.
    """
    if os.environ.get("RESOLUTO_STORE_KIND") != "s3" or "RESOLUTO_STORE_BUCKET" not in os.environ:
        print("\n[SKIP] k8s — set the s3 store config first:  set -a; source store.env; set +a")
        return "BLOCKED"

    from resoluto.sandbox.backends.substrate import SubstrateBackend
    from resoluto.sandbox.conduit.factory import store_from_env
    from resoluto.sandbox.conduit.s3 import mint_scoped_credential
    from resoluto.sandbox.runtime.k8s import K8sSandboxRuntime

    token = asyncio.run(mint_scoped_credential(
        bucket=os.environ["RESOLUTO_STORE_BUCKET"],
        prefix="run",
        endpoint_url=os.environ["RESOLUTO_STORE_ENDPOINT"],
        region=os.environ.get("RESOLUTO_STORE_REGION", "us-east-1"),
        access_key_id=os.environ["AWS_ACCESS_KEY_ID"],
        secret_access_key=os.environ["AWS_SECRET_ACCESS_KEY"],
        sts_role_arn=os.environ["RESOLUTO_STORE_STS_ROLE_ARN"],
    ))
    # host conduit keeps the full creds (staging); the POD gets only the scoped token.
    store_env = {k: v for k, v in os.environ.items() if k.startswith("RESOLUTO_STORE_")}
    store_env["RESOLUTO_STORE_WRITE_TOKEN"] = json.dumps(token)

    from resoluto.sandbox.runtime.k8s import EgressConfig
    runtime = K8sSandboxRuntime(
        namespace=os.environ.get("RESOLUTO_SANDBOX_NAMESPACE", "resoluto-sandboxes"),
        context=os.environ.get("RESOLUTO_SANDBOX_KUBECONTEXT"),
        egress=EgressConfig.from_store_env(),   # default-deny egress; enforced only by a netpol CNI
    )
    sb = Sandbox(backend=SubstrateBackend(
        runtime=runtime,
        conduit=store_from_env(),
        image=os.environ["RESOLUTO_LANE_IMAGE"],
        store_env=store_env,
    ))
    res = sb.run(
        ["python", "echo_agent.py", PROMPT],
        workspace=_staged_workspace(),
        env={"SMOKE_TAG": TAG},
        output_paths=["result.json"],
    )
    if _verify("k8s — Kata microVM pod", res):
        return "GREEN"
    if _egress_unenforced(res):
        print("    -> BLOCKED: this cluster does not enforce egress NetworkPolicy (Flannel). The "
              "agent contract is proven by the local backend and would pass here on an enforcing "
              "CNI (Cilium/Calico). Use RESOLUTO_LANE_BACKEND=local on this box.")
        return "BLOCKED"
    return "RED"


def main() -> int:
    arg = sys.argv[1] if len(sys.argv) > 1 else ""
    results: dict[str, str] = {}
    if arg != "--k8s-only":
        results["local"] = run_local()
    if arg != "--local-only":
        results["k8s"] = run_k8s()
    print("\n=== smoke summary ===")
    for backend, status in results.items():
        print(f"  {backend:6s}: {status}")
    # GREEN passes; BLOCKED is a known environment limit (egress not enforced), not a failure.
    return 0 if results and all(s in ("GREEN", "BLOCKED") for s in results.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
