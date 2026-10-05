#!/bin/sh
set -eu
graph=$1
backend=$2
capacity=$3
fail() { printf '%s\n' "$1" >&2; exit 1; }
validate_overlay() {
    check=$graph/.overlay-validation-$$
    mkdir "$check"
    check_created=1
    mkdir "$check/lower" "$check/upper" "$check/work" "$check/merged"
    printf 'lower\n' > "$check/lower/value"
    mount -t overlay overlay -o "lowerdir=$check/lower,upperdir=$check/upper,workdir=$check/work" "$check/merged"
    check_overlay=1
    printf 'upper\n' > "$check/merged/value"
    [ "$(cat "$check/lower/value")" = lower ] && [ "$(cat "$check/upper/value")" = upper ] || fail "overlay copy-on-write validation failed"
    umount "$check/merged"
    check_overlay=0
    rm -rf "$check"
    check_created=0
}
case "$backend" in
    block|tmpfs) ;;
    *) fail "unsupported graph backend: $backend" ;;
esac
if [ "$backend" = tmpfs ]; then
    [ "$(findmnt -n -o FSTYPE -T "$graph")" = tmpfs ] || fail "tmpfs graph requires tmpfs"
    printf 'overlay2\n'
    exit 0
fi
case "$capacity" in
    ''|*[!0-9]*) fail "positive graph capacity required" ;;
esac
[ "$capacity" -ge 67108864 ] || fail "positive graph capacity required (minimum 64MiB)"
[ "$(findmnt -n -o FSTYPE -T "$graph")" != tmpfs ] || fail "block graph cannot use tmpfs"
image=$graph/.resoluto-graph.ext4
created_image=0
mounted=0
check_overlay=0
check_created=0
loop=""
created_nodes=""
cleanup() {
    if [ "$check_overlay" = 1 ]; then umount "$check/merged" || return 1; fi
    if [ "$check_created" = 1 ]; then rm -rf "$check"; fi
    if [ "$mounted" = 1 ]; then umount "$graph" || return 1; fi
    if [ -n "$loop" ]; then losetup -d "$loop" || return 1; fi
    if [ "$created_image" = 1 ]; then rm "$image"; fi
    for node in $created_nodes; do rm "$node"; done
}
trap cleanup EXIT
source=$(findmnt -n -o SOURCE -T "$graph")
if [ -b "$source" ] && [ "$(findmnt -n -o TARGET -T "$graph")" = "$graph" ]; then
    [ "$(findmnt -n -o FSTYPE -T "$graph")" = ext4 ] || fail "existing block graph is not ext4"
    [ "$(blockdev --getsize64 "$source")" = "$capacity" ] || fail "existing block capacity differs"
    validate_overlay
    trap - EXIT
    printf 'overlay2\n'
    exit 0
fi
if [ -e "$image" ]; then
    [ -f "$image" ] && [ ! -L "$image" ] || fail "graph image must be a regular file"
    [ "$(stat -c %s "$image")" = "$capacity" ] || fail "existing graph capacity differs"
    [ "$(blkid -p -o value -s TYPE "$image")" = ext4 ] || fail "existing graph image is not ext4"
else
    [ -z "$(find "$graph" -mindepth 1 -maxdepth 1 -print -quit)" ] || fail "graph directory is not empty"
    free_bytes=$(df -B1 --output=avail "$graph" | tail -n 1 | tr -d ' ')
    [ "$free_bytes" -ge "$capacity" ] || fail "insufficient host graph capacity"
    (set -C; : > "$image")
    created_image=1
    truncate -s "$capacity" "$image"
    mkfs.ext4 -q -F "$image" >&2
fi
if [ ! -e /dev/loop-control ]; then
    mknod /dev/loop-control c 10 237
    created_nodes="$created_nodes /dev/loop-control"
fi
for device in /sys/class/block/loop*; do
    [ -e "$device/dev" ] || continue
    node=/dev/${device##*/}
    if [ ! -e "$node" ]; then
        numbers=$(cat "$device/dev")
        mknod "$node" b "${numbers%:*}" "${numbers#*:}"
        created_nodes="$created_nodes $node"
    fi
done
loop=$(losetup --find --show "$image")
mount -t ext4 "$loop" "$graph"
mounted=1
[ "$(findmnt -n -o FSTYPE -T "$graph")" = ext4 ] || fail "guest graph is not ext4"
validate_overlay
losetup -d "$loop"
trap - EXIT
printf 'overlay2\n'
