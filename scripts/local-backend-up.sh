#!/usr/bin/env bash
# local-backend-up.sh — bring up + verify the standalone `local` sandbox backend.
# Idempotent: check-and-repair every precondition, end with a green Kata-microVM canary.
set -euo pipefail

# tunables (env-overridable)
PREFIX="${RESOLUTO_LOCAL_PREFIX:-/opt/resoluto-local}"
CONTAINERD_SOCK="${RESOLUTO_LOCAL_CONTAINERD_ADDRESS:-/run/resoluto-local/containerd/containerd.sock}"
CONTAINERD_ROOT="${RESOLUTO_LOCAL_CONTAINERD_ROOT:-/var/lib/resoluto-local/containerd}"
NAMESPACE="${RESOLUTO_LOCAL_CONTAINERD_NAMESPACE:-resoluto-local}"
KATA_RUNTIME="${RESOLUTO_LOCAL_KATA_RUNTIME:-io.containerd.kata.v2}"
NET_NAME="${RESOLUTO_LOCAL_NETWORK:-resoluto-local}"
NET_SUBNET="${RESOLUTO_LOCAL_NET_SUBNET:-10.222.0.0/24}"
NET_GW="${RESOLUTO_LOCAL_NET_GW:-10.222.0.1}"
# Base image tag tracks the INSTALLED wheel (version_guard requires major.minor parity, and
# default_local_image()/the examples resolve `resoluto-sandbox-base:<wheel version>`) — never a
# hardcoded tag that silently drifts from the wheel across releases.
SANDBOX_VER="${RESOLUTO_SANDBOX_VERSION:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && uv run python -c 'import importlib.metadata as m; print(m.version("resoluto-sandbox"))' 2>/dev/null || echo 0.1.0)}"
SANDBOX_IMAGE="${RESOLUTO_SANDBOX_IMAGE:-localhost:5000/resoluto-sandbox-base:$SANDBOX_VER}"
CNI_NETCONF="${RESOLUTO_LOCAL_CNI_NETCONFPATH:-/etc/resoluto-local/cni/net.d}"
CNI_BIN="${RESOLUTO_LOCAL_CNI_PATH:-$PREFIX/libexec/cni}"
NERDCTL="$PREFIX/bin/nerdctl"
IMDS_CIDR="169.254.0.0/16"
# the OSS sandbox package — owns the SHARED, backend-neutral egress renderer (resoluto.sandbox.egress).
# This script lives at <resoluto-sandbox repo root>/scripts/, so the package root is one dir up.
SANDBOX_DIR="${RESOLUTO_SANDBOX_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"

red()   { printf "\033[31m%s\033[0m\n" "$*"; }
green() { printf "\033[32m%s\033[0m\n" "$*"; }
step()  { printf "\n\033[36m▶ %s\033[0m\n" "$*"; }
die()   { red "RED: $*"; exit 1; }

N() { sudo env PATH="/usr/local/bin:$PREFIX/bin:/usr/bin:/bin" "$NERDCTL" \
        --address "$CONTAINERD_SOCK" --namespace "$NAMESPACE" \
        --cni-path "$CNI_BIN" --cni-netconfpath "$CNI_NETCONF" "$@"; }

# 1. host prerequisites (Kata + KVM + the nerdctl bundle)
step "1/7 host prerequisites"
[ -e /dev/kvm ] || die "/dev/kvm missing — this host can't run Kata microVMs"
[ -x /opt/kata/bin/containerd-shim-kata-v2 ] || die "Kata not installed at /opt/kata (static install expected)"
[ -x "$PREFIX/bin/containerd" ] && [ -x "$NERDCTL" ] || die "nerdctl bundle missing at $PREFIX (install nerdctl-full there)"
ln -sf /opt/kata/bin/containerd-shim-kata-v2 /usr/local/bin/containerd-shim-kata-v2 2>/dev/null \
  || sudo ln -sf /opt/kata/bin/containerd-shim-kata-v2 /usr/local/bin/containerd-shim-kata-v2
sudo /opt/kata/bin/kata-runtime check >/dev/null 2>&1 || die "kata-runtime check failed"
green "ok: kata + kvm + nerdctl bundle present"

# scoped passwordless sudoers rule for the local-backend nerdctl binary (non-root user)
LOCAL_USER="${RESOLUTO_LOCAL_USER:-${SUDO_USER:-$USER}}"
if [ -n "$LOCAL_USER" ] && [ "$LOCAL_USER" != "root" ]; then
  SUDOERS=/etc/sudoers.d/resoluto-local-nerdctl
  printf '%s ALL=(root) NOPASSWD: %s\n' "$LOCAL_USER" "$NERDCTL" | sudo tee "$SUDOERS" >/dev/null
  sudo chmod 0440 "$SUDOERS"
  sudo visudo -cf "$SUDOERS" >/dev/null || die "invalid sudoers rule at $SUDOERS"
  green "ok: passwordless 'sudo -n nerdctl' for user '$LOCAL_USER' ($SUDOERS)"
else
  green "ok: running as root — no sudoers rule needed"
fi

# 2. dedicated containerd config + systemd unit
step "2/7 dedicated containerd (own socket/root, CRI off)"
sudo mkdir -p "$PREFIX/etc" "$CONTAINERD_ROOT" "$(dirname "$CONTAINERD_SOCK")"
sudo tee "$PREFIX/etc/containerd.toml" >/dev/null <<EOF
version = 2
root  = "$CONTAINERD_ROOT"
state = "$(dirname "$CONTAINERD_SOCK")"
disabled_plugins = ["io.containerd.grpc.v1.cri"]
[grpc]
  address = "$CONTAINERD_SOCK"
EOF
sudo tee /etc/systemd/system/resoluto-local-containerd.service >/dev/null <<EOF
[Unit]
Description=Resoluto local-backend dedicated containerd (Kata microVMs)
After=network.target
[Service]
ExecStart=$PREFIX/bin/containerd --config $PREFIX/etc/containerd.toml
Environment=PATH=/usr/local/bin:$PREFIX/bin:/usr/bin:/bin
Restart=always
Delegate=yes
KillMode=process
[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload
sudo systemctl enable --now resoluto-local-containerd.service
for _ in $(seq 1 30); do [ -S "$CONTAINERD_SOCK" ] && break; sleep 0.3; done
[ -S "$CONTAINERD_SOCK" ] || die "dedicated containerd socket never appeared ($CONTAINERD_SOCK)"
green "ok: resoluto-local-containerd running on $CONTAINERD_SOCK"

# 3. dedicated CNI bridge network (known subnet)
step "3/7 sandbox bridge network ($NET_NAME $NET_SUBNET)"
sudo mkdir -p "$CNI_NETCONF"
sudo tee "$CNI_NETCONF/10-$NET_NAME.conflist" >/dev/null <<EOF
{
  "cniVersion": "1.0.0",
  "name": "$NET_NAME",
  "plugins": [
    {"type": "bridge", "bridge": "resoluto-sbx0", "isGateway": true, "ipMasq": true,
     "ipam": {"type": "host-local", "ranges": [[{"subnet": "$NET_SUBNET", "gateway": "$NET_GW"}]],
              "routes": [{"dst": "0.0.0.0/0"}]}}
  ]
}
EOF
green "ok: CNI conflist written ($NET_NAME)"

# 4. host-side egress firewall on the sandbox bridge (immune to in-guest root)
step "4/7 egress firewall (default-deny; allow DNS+443-public; DROP IMDS+private)"
CHAIN="RESOLUTO-SANDBOX-EGRESS"
# Rules come from the SHARED, backend-neutral renderer (resoluto.sandbox.egress) so the `local`
# allowlist matches the `k8s` one. Configure via RESOLUTO_EGRESS_ALLOW (comma list of host/CIDR),
# RESOLUTO_EGRESS_ALLOW_PORT, RESOLUTO_EGRESS_PUBLIC_HTTPS. Default: DNS + all public :443; deny
# IMDS + RFC1918. Capture-then-apply so a render failure never leaves a half-built (deny-all) chain.
EGRESS_RULES="$(uv run --project "$SANDBOX_DIR" python -m resoluto.sandbox.egress local-iptables --chain "$CHAIN")"
[ -n "$EGRESS_RULES" ] || die "egress renderer produced no rules — refusing to touch the firewall"
sudo iptables -N "$CHAIN" 2>/dev/null || sudo iptables -F "$CHAIN"
while IFS= read -r rule; do
  [ -n "$rule" ] && sudo iptables $rule
done <<< "$EGRESS_RULES"
sudo iptables -C FORWARD -s "$NET_SUBNET" -j "$CHAIN" 2>/dev/null \
  || sudo iptables -I FORWARD 1 -s "$NET_SUBNET" -j "$CHAIN"
green "ok: egress firewall installed on $NET_SUBNET (shared renderer; allow='${RESOLUTO_EGRESS_ALLOW:-}')"

# 4b. SNI egress proxy (persistent) — reads its allowlist LIVE from a file that each Sandbox.run()
# rewrites (PER-STEP egress on the fly, no re-provision). This is one-time infra: the proxy + the
# static :443 redirect. Empty file = deny-all (secure). The runtime writes the file per run.
step "4b/7 SNI egress proxy (live per-run allowlist file)"
PROXY_PORT="${RESOLUTO_EGRESS_PROXY_PORT:-3129}"
DOMAINS_FILE="${RESOLUTO_LOCAL_EGRESS_DOMAINS_FILE:-/run/resoluto-local/egress-domains}"
PROXY_PID="/tmp/resoluto-egress-proxy.pid"
sudo mkdir -p "$(dirname "$DOMAINS_FILE")"
# Seed the SNI allowlist with the COMPLETE set every green run needs (agent LLM + the dev-env
# `docker compose build`: registries, pip, npm, git, and apt/nodesource/playwright over HTTPS —
# apt uses https:// mirrors so :80 stays REJECT'd and the fail-closed canary stays green).
# Durable here so a fresh machine gets the full list; override with RESOLUTO_EGRESS_DOMAINS.
DEFAULT_EGRESS_DOMAINS="api.anthropic.com,*.anthropic.com,registry.npmjs.org,*.npmjs.org,pypi.org,*.pypi.org,files.pythonhosted.org,github.com,*.github.com,*.githubusercontent.com,sentry.io,*.sentry.io,*.statsig.com,registry-1.docker.io,*.docker.io,auth.docker.io,*.docker.com,ghcr.io,deb.debian.org,security.debian.org,deb.nodesource.com,cdn.playwright.dev,storage.googleapis.com,*.googleapis.com,edgedl.me.gvt1.com,*.gvt1.com"
printf '%s' "${RESOLUTO_EGRESS_DOMAINS:-$DEFAULT_EGRESS_DOMAINS}" | sudo tee "$DOMAINS_FILE" >/dev/null
sudo chown "$(id -u):$(id -g)" "$DOMAINS_FILE"
[ -f "$PROXY_PID" ] && kill "$(cat "$PROXY_PID")" 2>/dev/null; rm -f "$PROXY_PID"
pkill -f 'resoluto.sandbox.egress_proxy' 2>/dev/null || true
RESOLUTO_EGRESS_DOMAINS_FILE="$DOMAINS_FILE" setsid bash -c \
  "cd '$SANDBOX_DIR' && exec uv run python -m resoluto.sandbox.egress_proxy --host 0.0.0.0 --port $PROXY_PORT" \
  >/tmp/resoluto-egress-proxy.log 2>&1 &
echo $! > "$PROXY_PID"
sleep 2
kill -0 "$(cat "$PROXY_PID")" 2>/dev/null || die "SNI proxy failed to start (see /tmp/resoluto-egress-proxy.log)"
sudo iptables -C INPUT -s "$NET_SUBNET" -p tcp --dport "$PROXY_PORT" -j ACCEPT 2>/dev/null \
  || sudo iptables -I INPUT 1 -s "$NET_SUBNET" -p tcp --dport "$PROXY_PORT" -j ACCEPT
sudo iptables -t nat -C PREROUTING -s "$NET_SUBNET" -p tcp --dport 443 -j REDIRECT --to-port "$PROXY_PORT" 2>/dev/null \
  || sudo iptables -t nat -I PREROUTING 1 -s "$NET_SUBNET" -p tcp --dport 443 -j REDIRECT --to-port "$PROXY_PORT"
green "ok: SNI proxy up (:$PROXY_PORT); sandbox :443 filtered by $DOMAINS_FILE (set per-run via Sandbox.run(egress=[...]))"

# 5. sandbox image present in the dedicated containerd namespace
step "5/7 sandbox image ($SANDBOX_IMAGE)"
if ! N images "$SANDBOX_IMAGE" 2>/dev/null | grep -q resoluto-sandbox; then
  if ! N pull --quiet --insecure-registry "$SANDBOX_IMAGE" 2>/dev/null; then
    # Not in the registry yet — build the base from Dockerfile.base and push it, then pull. This is
    # what makes `local-backend-up.sh` a true one-command bring-up (no hidden "build the image first").
    command -v docker >/dev/null || die "$SANDBOX_IMAGE is not in the registry and docker is missing — build it (\`resoluto-sandbox image build\`) and push it to localhost:5000, then re-run."
    green "image not in registry — building $SANDBOX_IMAGE from Dockerfile.base (first run only)"
    ( cd "$SANDBOX_DIR" && docker build -f Dockerfile.base -t "$SANDBOX_IMAGE" . && docker push "$SANDBOX_IMAGE" ) \
      || die "failed to build/push $SANDBOX_IMAGE from Dockerfile.base"
    N pull --quiet --insecure-registry "$SANDBOX_IMAGE" || die "could not pull $SANDBOX_IMAGE into $NAMESPACE after build"
  fi
fi
green "ok: sandbox image available"

# 6. write the local-backend env file
step "6/7 local.env"
ENV_FILE="${RESOLUTO_LOCAL_ENV_FILE:-$(dirname "$0")/../local.env}"
cat > "$ENV_FILE" <<EOF
# Generated by local-backend-up.sh — auto-loaded by the CLI/backend, or: set -a; source local.env; set +a
RESOLUTO_LOCAL_CONTAINERD_ADDRESS=$CONTAINERD_SOCK
RESOLUTO_LOCAL_CONTAINERD_NAMESPACE=$NAMESPACE
RESOLUTO_LOCAL_KATA_RUNTIME=$KATA_RUNTIME
RESOLUTO_LOCAL_NETWORK=$NET_NAME
RESOLUTO_LOCAL_CNI_PATH=$CNI_BIN
RESOLUTO_LOCAL_CNI_NETCONFPATH=$CNI_NETCONF
RESOLUTO_LOCAL_NERDCTL=$NERDCTL
RESOLUTO_SANDBOX_IMAGE=$SANDBOX_IMAGE
EOF
green "ok: wrote $ENV_FILE"

# 7. canary: a Kata microVM boots + egress is enforced
step "7/7 canary: boot a Kata microVM and verify egress enforcement"
OUT=$(N run --rm --network "$NET_NAME" --runtime "$KATA_RUNTIME" "$SANDBOX_IMAGE" python3 -c '
import socket
print("guest_kernel=" + __import__("os").uname().release)
def reachable(host, port):
    try:
        socket.create_connection((host, port), timeout=5).close(); return True
    except Exception:
        return False
# ALLOWED egress: DNS (:53) is always permitted, so 1.1.1.1:53 (Cloudflare answers DNS over TCP/53)
# must SUCCEED — this proves the network is alive AND the allow path works (not a false GREEN from a
# VM with no network at all). Everything else is DENIED by default (secure): IMDS, non-allowed ports,
# and public :443 too (open it deliberately with RESOLUTO_EGRESS_PUBLIC_HTTPS=1 or RESOLUTO_EGRESS_ALLOW).
print("DNS53=" + ("REACHABLE" if reachable("1.1.1.1", 53) else "blocked"))
print("IMDS=" + ("REACHABLE" if reachable("169.254.169.254", 80) else "blocked"))
print("PORT80=" + ("REACHABLE" if reachable("1.1.1.1", 80) else "blocked"))
print("HTTPS443=" + ("REACHABLE" if reachable("1.1.1.1", 443) else "blocked"))
' 2>&1) || die "canary microVM failed to run: $OUT"
echo "$OUT" | sed "s/^/    /"
echo "$OUT" | grep -q "guest_kernel=" || die "microVM did not boot"
echo "$OUT" | grep -q "DNS53=REACHABLE" || die "DNS blocked — VM has no usable network (the allow path is broken)"
echo "$OUT" | grep -q "IMDS=blocked" || die "IMDS reachable — egress firewall NOT enforced"
echo "$OUT" | grep -q "PORT80=blocked" || die "non-allowed egress reachable — firewall NOT default-deny"
green "GREEN: local Kata backend up — VM isolation + egress DENY-by-default enforced (443='$(echo "$OUT" | sed -n 's/HTTPS443=//p')'). Open egress with RESOLUTO_EGRESS_ALLOW / _PUBLIC_HTTPS."
