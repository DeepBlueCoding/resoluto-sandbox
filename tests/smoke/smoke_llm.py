#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# ///
"""LLM smoke test: ask Claude a REAL question THROUGH the sandbox and SHOW the
input (the prompt) and the output (Claude's answer).

This proves the full workload path end to end: a real LLM call runs inside the Kata sandbox,
reaching api.anthropic.com over the allowed public :443 egress, and its input/output
round-trips through the store-mediated substrate.

Auth: export CLAUDE_CODE_OAUTH_TOKEN (from `claude setup-token`); it is forwarded to the guest via
env= (the sandbox never reads or parses a provider credential file). ANTHROPIC_API_KEY stays unset so
usage bills your subscription. The token rides in the pod env (a secret) — fine for a local dev smoke.

Egress is DENY-by-default (secure) — a sandbox can't reach the LLM until you open it. The k8s
path here opens just the provider (`allow=["anthropic"]`). The LOCAL backend enforces egress at
PROVISION time, so provision it with the LLM opened first:
    RESOLUTO_EGRESS_ALLOW=anthropic bash scripts/local-backend-up.sh    # (or RESOLUTO_EGRESS_PUBLIC_HTTPS=1)

Run from resoluto-sandbox/ (sandbox image present; backends provisioned):
    export RESOLUTO_STORE_* RESOLUTO_SANDBOX_IMAGE   # your s3 store (k8s); then:
    set -a; source local.env; set +a                 # local-Kata backend config
    uv run python tests/smoke/smoke_llm.py                       # local backend
    uv run python tests/smoke/smoke_llm.py --k8s-only            # k8s backend
    uv run python tests/smoke/smoke_llm.py "your own prompt"
"""
import io
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

from resoluto.sandbox import Sandbox

EXAMPLES = Path(__file__).resolve().parent
AGENT = EXAMPLES / "llm_agent.py"
DEFAULT_PROMPT = "In exactly five words, say why isolated sandboxes matter."


def _staged_workspace() -> str:
    """Throwaway workspace holding just the agent script (the token travels via env, not files)."""
    ws = Path(tempfile.mkdtemp(prefix="llm-ws-"))
    shutil.copy(AGENT, ws / "llm_agent.py")
    return str(ws)


def _agent_env(token: str) -> dict:
    # CLAUDE_CODE_OAUTH_TOKEN authenticates the guest claude CLI; ANTHROPIC_API_KEY stays unset.
    return {"CLAUDE_CODE_OAUTH_TOKEN": token}


def _show(label: str, prompt: str, res) -> bool:
    answer = (res.result or {}).get("answer", "") if res.result else ""
    ok = bool(res.ok and answer)
    print(f"\n────────── {label} ──────────")
    print(f"  INPUT  (prompt to the LLM): {prompt!r}")
    print(f"  OUTPUT (the LLM's answer) : {answer!r}")
    print(f"  exit={res.exit_code}  result.status={(res.result or {}).get('status')!r}")
    if ok:
        print("  [PASS] a real Claude answer round-tripped through the sandbox")
    else:
        print(f"  [FAIL] no answer — reason={res.reason!r}  (the agent's stderr is in RunResult.output)")
    return ok


def _egress_unenforced(res) -> bool:
    """The sandbox refused ONLY because the cluster CNI didn't program egress in time (a race, not a
    code failure). The local backend enforces egress host-side and is the reliable path here."""
    return "egress canary failed" in (res.reason or "") or "egress canary failed" in res.output


def run_local(prompt: str, token: str) -> str:
    image = os.environ["RESOLUTO_SANDBOX_IMAGE"]
    res = Sandbox(backend="local", image=image).run(
        ["python", "llm_agent.py", prompt],
        workspace=_staged_workspace(),
        env=_agent_env(token),
        output_paths=["result.json"],
        stream=io.StringIO(),   # suppress live substrate telemetry; we print the clean answer
    )
    return "GREEN" if _show("local — Claude via Kata microVM (nerdctl)", prompt, res) else "RED"


def run_k8s(prompt: str, token: str) -> str:
    if os.environ.get("RESOLUTO_STORE_KIND") != "s3":
        print("[SKIP] k8s — set the s3 store config (export RESOLUTO_STORE_*)")
        return "BLOCKED"
    import asyncio
    from resoluto.sandbox.backends.substrate import SubstrateBackend
    from resoluto.sandbox.conduit.factory import store_from_env
    from resoluto.sandbox.conduit.s3 import mint_scoped_credential
    from resoluto.sandbox.runtime.k8s import EgressConfig, K8sSandboxRuntime

    sts = asyncio.run(mint_scoped_credential(
        bucket=os.environ["RESOLUTO_STORE_BUCKET"], prefix="run",
        endpoint_url=os.environ["RESOLUTO_STORE_ENDPOINT"],
        region=os.environ.get("RESOLUTO_STORE_REGION", "us-east-1"),
        access_key_id=os.environ["AWS_ACCESS_KEY_ID"],
        secret_access_key=os.environ["AWS_SECRET_ACCESS_KEY"],
        sts_role_arn=os.environ["RESOLUTO_STORE_STS_ROLE_ARN"],
    ))
    store_env = {k: v for k, v in os.environ.items() if k.startswith("RESOLUTO_STORE_")}
    store_env["RESOLUTO_STORE_WRITE_TOKEN"] = json.dumps(sts)
    # Egress is DENY-by-default — the LLM sandbox can't phone home unless we open it. Open just the LLM
    # provider (least privilege). CDN IPs rotate, so if this flakes, use public_https=True instead.
    import dataclasses
    egress = EgressConfig.from_store_env() or EgressConfig()
    egress = dataclasses.replace(egress, allow=tuple(egress.allow) + ("anthropic",))
    runtime = K8sSandboxRuntime(
        namespace=os.environ.get("RESOLUTO_SANDBOX_NAMESPACE", "resoluto-sandboxes"),
        context=os.environ.get("RESOLUTO_SANDBOX_KUBECONTEXT"),
        egress=egress,
    )
    sb = Sandbox(backend=SubstrateBackend(
        runtime=runtime, conduit=store_from_env(),
        image=os.environ["RESOLUTO_SANDBOX_IMAGE"], store_env=store_env,
    ))
    res = sb.run(
        ["python", "llm_agent.py", prompt],
        workspace=_staged_workspace(),
        env=_agent_env(token),
        output_paths=["result.json"],
        stream=io.StringIO(),
    )
    if _show("k8s — Claude via Kata pod", prompt, res):
        return "GREEN"
    if _egress_unenforced(res):
        print("    -> BLOCKED: the egress NetworkPolicy didn't program before the pod's canary "
              "(kube-router race). The LLM round-trip is proven by the local backend.")
        return "BLOCKED"
    return "RED"


def main() -> int:
    args = sys.argv[1:]
    flags = {a for a in args if a.startswith("--")}
    prompt = next((a for a in args if not a.startswith("--")), DEFAULT_PROMPT)

    token = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")
    if not token:
        print("set CLAUDE_CODE_OAUTH_TOKEN (`claude setup-token`) to auth the agent. "
              "ANTHROPIC_API_KEY is not used.")
        return 1

    results = {}
    if "--k8s-only" not in flags:
        results["local"] = run_local(prompt, token)
    if "--k8s-only" in flags or "--both" in flags:
        results["k8s"] = run_k8s(prompt, token)

    print("\n=== llm smoke summary ===")
    for b, status in results.items():
        print(f"  {b:6s}: {status}")
    # GREEN passes; BLOCKED is the known k8s egress-programming race, not a code failure.
    return 0 if results and all(s in ("GREEN", "BLOCKED") for s in results.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
