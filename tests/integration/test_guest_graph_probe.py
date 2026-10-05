import base64
import json
import os
import subprocess
import uuid
from pathlib import Path

import pytest


@pytest.mark.integration
def test_owned_kata_loop_ext4_overlay_preserves_lower_and_upper_bytes():
    pod = os.environ["RES58_GRAPH_PROBE_POD"]
    run_id = os.environ["RES58_GRAPH_PROBE_RUN"]
    namespace = "resoluto-sandboxes"
    metadata = json.loads(
        subprocess.check_output(
            ["kubectl", "get", "pod", "-n", namespace, pod, "-o", "json"], text=True
        )
    )
    assert metadata["metadata"]["labels"]["resoluto.run_id"] == run_id
    assert metadata["metadata"]["labels"]["resoluto.kind"] == "lane"
    token = uuid.uuid4().hex
    script = r"""
set -eu
probe=/var/lib/docker/.res58-graph-probe-$1
target=/tmp/res58-graph-probe-$1
created_nodes=""
loop=""
mounted=0
overlaid=0
cleanup() {
    if [ "$overlaid" = 1 ]; then umount "$target/merged" || return 1; fi
    if [ "$mounted" = 1 ]; then umount "$target" || return 1; fi
    if [ -n "$loop" ]; then losetup -d "$loop" || return 1; fi
    rm -rf "$probe" "$target"
    for node in $created_nodes; do rm "$node"; done
}
trap cleanup EXIT
mkdir "$probe" "$target"
if [ ! -e /dev/loop-control ]; then
    mknod /dev/loop-control c 10 237
    created_nodes="$created_nodes /dev/loop-control"
fi
for index in 0 1 2 3 4 5 6 7; do
    if [ ! -e /dev/loop$index ]; then
        mknod /dev/loop$index b 7 "$index"
        created_nodes="$created_nodes /dev/loop$index"
    fi
done
truncate -s 134217728 "$probe/graph.img"
mkfs.ext4 -q -F "$probe/graph.img"
loop=$(losetup --find --show "$probe/graph.img")
mount -t ext4 "$loop" "$target"
mounted=1
test "$(findmnt -n -o FSTYPE "$target")" = ext4
mkdir "$target/lower" "$target/upper" "$target/work" "$target/merged"
printf 'original-layer\n' > "$target/lower/value.txt"
mount -t overlay overlay -o "lowerdir=$target/lower,upperdir=$target/upper,workdir=$target/work" "$target/merged"
overlaid=1
test "$(cat "$target/merged/value.txt")" = original-layer
printf 'modified-layer\n' > "$target/merged/value.txt"
test "$(cat "$target/lower/value.txt")" = original-layer
test "$(cat "$target/upper/value.txt")" = modified-layer
printf 'ext4=verified overlay=verified lower=original-layer upper=modified-layer\n'
"""
    result = subprocess.run(
        ["kubectl", "exec", "-i", "-n", namespace, pod, "--", "sh", "-s", "--", token],
        input=script,
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip() == (
        "ext4=verified overlay=verified lower=original-layer upper=modified-layer"
    )
    cleanup_result = subprocess.run(
        [
            "kubectl",
            "exec",
            "-n",
            namespace,
            pod,
            "--",
            "sh",
            "-c",
            'test ! -e "$1" && test ! -e "$2"',
            "--",
            f"/var/lib/docker/.res58-graph-probe-{token}",
            f"/tmp/res58-graph-probe-{token}",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert cleanup_result.returncode == 0, cleanup_result.stderr


@pytest.mark.integration
def test_owned_guest_graph_helper_retains_existing_ext4_payload():
    pod = os.environ["RES58_GRAPH_PROBE_POD"]
    run_id = os.environ["RES58_GRAPH_PROBE_RUN"]
    namespace = "resoluto-sandboxes"
    metadata = json.loads(
        subprocess.check_output(
            ["kubectl", "get", "pod", "-n", namespace, pod, "-o", "json"], text=True
        )
    )
    assert metadata["metadata"]["labels"]["resoluto.run_id"] == run_id
    assert metadata["metadata"]["labels"]["resoluto.kind"] == "lane"
    helper = Path(__file__).resolve().parents[2] / "scripts/dind-graph.sh"
    payload = base64.b64encode(helper.read_bytes()).decode()
    token = uuid.uuid4().hex
    script = r"""
set -eu
probe=/var/lib/docker/.res58-graph-probe-$1
target=/tmp/res58-graph-probe-$1
before_nodes=$(find /dev -maxdepth 1 -name 'loop*' -print)
parent_net=$(readlink /proc/$$/ns/net)
main_pid=$(cat /var/run/docker.pid)
main_net=$(readlink /proc/$main_pid/ns/net)
docker0_index=""
if [ -r /sys/class/net/docker0/ifindex ]; then docker0_index=$(cat /sys/class/net/docker0/ifindex); fi
mounted=0
daemon=""
cleanup() {
    if [ -n "$daemon" ]; then
        if kill -0 "$daemon" 2>/dev/null; then kill -TERM "$daemon"; fi
        if wait "$daemon"; then :; else printf 'owned daemon cleanup status=%s\n' "$?" >&2; fi
    fi
    if [ "$mounted" = 1 ]; then umount "$probe/graph" || return 1; fi
    rm -rf "$probe" "$target"
    for node in /dev/loop*; do
        [ -e "$node" ] || continue
        if ! printf '%s\n' "$before_nodes" | grep -Fxq "$node"; then rm "$node"; fi
    done
}
trap cleanup EXIT
mkdir "$probe" "$target" "$probe/graph"
printf '%s' "$2" | base64 -d > "$target/helper.sh"
driver=$(sh "$target/helper.sh" "$probe/graph" block 134217728)
mounted=1
test "$driver" = overlay2
test "$(findmnt -n -o FSTYPE -T "$probe/graph")" = ext4
mkdir "$target/context" "$target/exec"
printf '{}\n' > "$target/daemon.json"
timeout --signal=TERM --kill-after=10s 120s unshare --net dockerd --config-file "$target/daemon.json" --data-root "$probe/graph" --exec-root "$target/exec" --pidfile "$target/dockerd.pid" --host "unix://$target/docker.sock" --storage-driver overlay2 --bridge none --iptables=false --ip6tables=false --ip-forward=false --ip-masq=false --userland-proxy=false > "$target/dockerd.log" 2>&1 &
daemon=$!
export DOCKER_HOST=unix://$target/docker.sock
for attempt in $(seq 1 30); do
    if docker info --format '{{.Driver}}' > "$target/driver" 2>/dev/null; then break; fi
    sleep 0.5
done
if [ ! -s "$target/driver" ]; then cat "$target/dockerd.log" >&2; exit 1; fi
test "$(cat "$target/driver")" = overlay2
own_pid=$(cat "$target/dockerd.pid")
own_net=$(readlink /proc/$own_pid/ns/net)
test "$own_net" != "$parent_net"
test "$own_net" != "$main_net"
printf 'payload-buildkit-loop\n' > "$target/context/value.txt"
printf 'FROM scratch AS source\nCOPY value.txt /value.txt\nFROM scratch\nCOPY --from=source /value.txt /value.txt\n' > "$target/context/Dockerfile"
DOCKER_BUILDKIT=1 docker build --network none -t res58-probe "$target/context" > "$target/build.log" 2>&1 || { cat "$target/build.log" >&2; exit 1; }
container=$(docker create --network none res58-probe /unused-command)
docker cp "$container:/value.txt" "$target/copied.txt"
test "$(cat "$target/copied.txt")" = payload-buildkit-loop
docker rm "$container" > /dev/null
kill -TERM "$daemon"
wait "$daemon" || test "$?" = 143
daemon=""
test "$(readlink /proc/$main_pid/ns/net)" = "$main_net"
if [ -n "$docker0_index" ]; then test "$(cat /sys/class/net/docker0/ifindex)" = "$docker0_index"; fi
printf 'retained-runtime-payload\n' > "$probe/graph/sentinel"
test "$(sh "$target/helper.sh" "$probe/graph" block 134217728)" = overlay2
test "$(cat "$probe/graph/sentinel")" = retained-runtime-payload
umount "$probe/graph"
mounted=0
image=$probe/graph/.resoluto-graph.ext4
test -z "$(losetup -j "$image")"
uuid_before=$(blkid -p -s UUID -o value "$image")
driver=$(sh "$target/helper.sh" "$probe/graph" block 134217728)
mounted=1
test "$driver" = overlay2
test "$(cat "$probe/graph/sentinel")" = retained-runtime-payload
umount "$probe/graph"
mounted=0
test "$(blkid -p -s UUID -o value "$image")" = "$uuid_before"
test -z "$(losetup -j "$image")"
if sh "$target/helper.sh" "$probe/graph" block 268435456 > "$target/mismatch.out" 2>&1; then exit 1; fi
grep -q 'existing graph capacity differs' "$target/mismatch.out"
printf 'helper=overlay2 retained=retained-runtime-payload UUID=unchanged capacity_mismatch=rejected autoclear=verified BuildKitCOPY=payload-buildkit-loop network=isolated main_network=unchanged\n'
"""
    result = subprocess.run(
        ["kubectl", "exec", "-i", "-n", namespace, pod, "--", "sh", "-s", "--", token, payload],
        input=script,
        text=True,
        capture_output=True,
        timeout=180,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert (
        result.stdout.strip()
        == "helper=overlay2 retained=retained-runtime-payload UUID=unchanged capacity_mismatch=rejected autoclear=verified BuildKitCOPY=payload-buildkit-loop network=isolated main_network=unchanged"
    )


@pytest.mark.integration
def test_owned_guest_graph_helper_stacks_ext4_over_existing_graph_mount():
    pod = os.environ["RES58_GRAPH_PROBE_POD"]
    run_id = os.environ["RES58_GRAPH_PROBE_RUN"]
    namespace = "resoluto-sandboxes"
    metadata = json.loads(
        subprocess.check_output(
            ["kubectl", "get", "pod", "-n", namespace, pod, "-o", "json"], text=True
        )
    )
    assert metadata["metadata"]["labels"]["resoluto.run_id"] == run_id
    assert metadata["metadata"]["labels"]["res58-diagnostic"] == "graph-mount"
    assert metadata["metadata"]["uid"] == os.environ["RES58_GRAPH_PROBE_UID"]
    helper = Path(__file__).resolve().parents[2] / "scripts/dind-graph.sh"
    payload = base64.b64encode(helper.read_bytes()).decode()
    token = uuid.uuid4().hex
    script = r"""
set -eu
mount --make-rprivate /
probe=/var/lib/docker/.res58-graph-probe-$1
target=/tmp/res58-stacked-graph-$1
before_nodes=$(find /dev -maxdepth 1 -name 'loop*' -print)
bound=0
mounted=0
cleanup() {
    if [ "$mounted" = 1 ]; then umount "$target/graph" || return 1; fi
    if [ "$bound" = 1 ]; then umount "$target/graph" || return 1; fi
    rm -rf "$probe" "$target"
    for node in /dev/loop*; do
        [ -e "$node" ] || continue
        if ! printf '%s\n' "$before_nodes" | grep -Fxq "$node"; then rm "$node"; fi
    done
}
trap cleanup EXIT
mkdir "$probe" "$target" "$target/graph"
printf '%s' "$2" | base64 -d > "$target/helper.sh"
mount --bind "$probe" "$target/graph"
bound=1
printf 'before mountinfo: ' >&2
cat /proc/self/mountinfo >&2
if driver=$(sh "$target/helper.sh" "$target/graph" block 134217728); then
    mounted=1
else
    printf 'failed mountinfo: ' >&2
    cat /proc/self/mountinfo >&2
    exit 1
fi
test "$driver" = overlay2
test "$(stat -f -c %T "$target/graph")" = ext2/ext3
printf 'after: ' >&2
findmnt -n -o SOURCE,FSTYPE,TARGET -T "$target/graph" >&2
cat /proc/self/mountinfo >&2
printf 'actual-top-ext4\n' > "$target/graph/sentinel"
test "$(sh "$target/helper.sh" "$target/graph" block 134217728)" = overlay2
test "$(cat "$target/graph/sentinel")" = actual-top-ext4
umount "$target/graph"
mounted=0
test -z "$(losetup -j "$probe/.resoluto-graph.ext4")"
printf 'stacked_graph=ext4 driver=overlay2 retained=actual-top-ext4 autoclear=verified\n'
"""
    result = subprocess.run(
        [
            "kubectl",
            "exec",
            "-n",
            namespace,
            pod,
            "--",
            "unshare",
            "--mount",
            "sh",
            "-c",
            script,
            "--",
            token,
            payload,
        ],
        text=True,
        capture_output=True,
        timeout=180,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip() == (
        "stacked_graph=ext4 driver=overlay2 retained=actual-top-ext4 autoclear=verified"
    )
