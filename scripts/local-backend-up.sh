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

# Defensive: a STALE bridge owning the same subnet (e.g. a pre-rename `resoluto-lane0`) leaves a
# DUPLICATE `$NET_SUBNET` route. The kernel may pick the dead bridge for RETURN traffic and silently
# black-hole every reply — DNS/HTTPS time out from the guest even though the firewall allows them, and
# the canary fails with all-egress-blocked. Remove any bridge (other than the current sandbox bridge)
# that owns this subnet's route.
for stale in $(ip -o route show "$NET_SUBNET" 2>/dev/null \
    | awk '{for(i=1;i<=NF;i++) if($i=="dev") print $(i+1)}' | grep -vx "resoluto-sbx0" | sort -u); do
  if [ -d "/sys/class/net/$stale/bridge" ]; then
    red "removing STALE bridge '$stale' — its duplicate $NET_SUBNET route black-holes sandbox return traffic"
    sudo ip link delete "$stale" type bridge 2>/dev/null || true
  fi
done

# 4. egress is RUNTIME-MANAGED, not provisioned here.
# A non-empty `Sandbox.run(egress=[...])` makes KataNerdctlSandboxRuntime start a per-run SNI proxy +
# scoped iptables and tear them down when the run ends — no persistent firewall, no persistent proxy,
# nothing to set up here (and nothing to leak/collide with). The secure default is `--network none`.
step "4/5 egress: runtime-managed per run (no host firewall provisioned)"
green "ok: egress is granted per run via Sandbox.run(egress=[...]); default = no network"

# ensure the on-box registry the image bridge uses (localhost:5000): `image build` and this script
# push there and the backend pulls from it. Docker is a documented prereq; reuse or start a registry:2.
case "$SANDBOX_IMAGE" in
  localhost:5000/*)
    if ! curl -fsS http://localhost:5000/v2/ >/dev/null 2>&1; then
      command -v docker >/dev/null || die "registry localhost:5000 is down and docker is missing to start it — run: docker run -d --restart unless-stopped -p 5000:5000 --name registry registry:2"
      if docker inspect resoluto-registry >/dev/null 2>&1; then docker start resoluto-registry >/dev/null
      elif docker inspect registry >/dev/null 2>&1; then docker start registry >/dev/null
      else docker run -d --restart unless-stopped -p 5000:5000 --name resoluto-registry registry:2 >/dev/null; fi
      curl -fsS http://localhost:5000/v2/ >/dev/null 2>&1 || die "started a registry but localhost:5000 is still unreachable"
      green "ok: started local registry (localhost:5000)"
    fi ;;
esac

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

# 7. canary: a Kata microVM boots on the SECURE DEFAULT (no network) and is fully isolated
step "5/5 canary: boot a Kata microVM (secure default = no network) and verify isolation"
OUT=$(N run --rm --network none --runtime "$KATA_RUNTIME" "$SANDBOX_IMAGE" python3 -c '
import socket
print("guest_kernel=" + __import__("os").uname().release)
def reachable(host, port):
    try:
        socket.create_connection((host, port), timeout=5).close(); return True
    except Exception:
        return False
# Secure default: a run with no egress allowlist gets NO network interface — nothing is reachable.
# Per-run egress is granted by Sandbox.run(egress=[...]) and enforced by the runtime, not provisioned here.
print("DNS53=" + ("REACHABLE" if reachable("1.1.1.1", 53) else "blocked"))
print("IMDS=" + ("REACHABLE" if reachable("169.254.169.254", 80) else "blocked"))
print("HTTPS443=" + ("REACHABLE" if reachable("1.1.1.1", 443) else "blocked"))
' 2>&1) || die "canary microVM failed to run: $OUT"
echo "$OUT" | sed "s/^/    /"
echo "$OUT" | grep -q "guest_kernel=" || die "microVM did not boot"
echo "$OUT" | grep -q "DNS53=blocked" || die "network reachable on the no-egress default — isolation NOT enforced"
echo "$OUT" | grep -q "IMDS=blocked" || die "IMDS reachable — isolation NOT enforced"
echo "$OUT" | grep -q "HTTPS443=blocked" || die "public HTTPS reachable on the no-egress default — isolation NOT enforced"
green "GREEN: local Kata backend up — VM isolation + no-network secure default. Grant egress per run via Sandbox.run(egress=[...])."
