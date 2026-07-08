#!/usr/bin/env bash
# release-preflight.sh — verify a package is ready to release BEFORE the tag is pushed.
#
# Usage:  scripts/release-preflight.sh <vX.Y.Z | vX.Y.Z-rcN> [--full]
#   Fast checks (always): tag format, pyproject↔tag version match, not-already-released,
#                         changelog entry present, clean working tree.
#   --full also runs what CI runs, locally: uv build + twine check + unit tests.
# Exit 0 = ready to release; non-zero = blocked (prints the reason).
#
# This is the single source of truth for "is this version releasable". The pre-push hook
# (.githooks/pre-push) calls it automatically on every `git push` of a v* tag; run it by hand
# with --full before you tag.
set -euo pipefail

RED=$'\e[31m'; GRN=$'\e[32m'; YEL=$'\e[33m'; RST=$'\e[0m'
fail() { echo "${RED}✗ release blocked:${RST} $*" >&2; exit 1; }
ok()   { echo "${GRN}✓${RST} $*"; }

[ $# -ge 1 ] || fail "usage: release-preflight.sh <vX.Y.Z> [--full]"
raw="$1"; shift || true
FULL=false; [ "${1:-}" = "--full" ] && FULL=true

root="$(git rev-parse --show-toplevel)"
cd "$root"
[ -f pyproject.toml ] || fail "no pyproject.toml at repo root ($root)"

# 1. tag format — vX.Y.Z, optional -rcN / .devN pre-release suffix
tag="${raw#refs/tags/}"
case "$tag" in
  v[0-9]*.[0-9]*.[0-9]*) : ;;
  *) fail "tag '$tag' is not vX.Y.Z (e.g. v0.1.0, v0.2.0-rc1)";;
esac
ver="${tag#v}"                                   # 0.1.0  or  0.1.0-rc1
prerelease=false
case "$ver" in *-*|*.dev*) prerelease=true;; esac
base="${ver%%-*}"                                # 0.1.0 (drop pre-release suffix)
ok "tag $tag → $([ $prerelease = true ] && echo 'TestPyPI (pre-release)' || echo 'PyPI (final)')"

# 2. pyproject version must equal the tag (PEP440-normalized: -rcN→rcN, -devN→.devN)
pyver="$(grep -m1 '^version *= *' pyproject.toml | sed -E 's/^version *= *"([^"]+)".*/\1/')"
norm="$(printf '%s' "$ver" | sed -E 's/-rc/rc/; s/-dev/.dev/; s/-//g')"
[ "$pyver" = "$norm" ] || fail "version mismatch — tag $tag implies pyproject version '$norm', but pyproject.toml says '$pyver'. Bump pyproject.toml (or fix the tag)."
ok "pyproject version matches tag: $pyver"

# 3. not already released — the tag must not already exist on origin
if git ls-remote --exit-code --tags origin "refs/tags/$tag" >/dev/null 2>&1; then
  fail "$tag already exists on origin — you are re-releasing an existing version. Bump it."
fi
ok "$tag is a fresh release (not on origin)"

# 4. changelog has a real section for this version (not just 'Unreleased')
cl="docs/changelog.md"; [ -f "$cl" ] || cl="CHANGELOG.md"
if [ -f "$cl" ]; then
  if grep -qiE "^##[[:space:]]+\[?${base}([[:space:]]|\]|$)" "$cl"; then
    ok "changelog has an entry for $base ($cl)"
  else
    fail "no changelog entry for $base in $cl — promote the 'Unreleased' section to '## $base'."
  fi
else
  echo "${YEL}!${RST} no changelog file ($cl) — skipping changelog check"
fi

# 5. clean working tree — release from a committed state
[ -z "$(git status --porcelain)" ] || fail "working tree is dirty — commit or stash before releasing."
ok "working tree clean"

if $FULL; then
  echo "── --full: build + metadata + unit tests (what CI runs) ──"
  rm -rf dist
  uv build >/dev/null 2>&1 && ok "uv build" || fail "uv build failed (run 'uv build' to see why)"
  uvx twine check dist/* >/dev/null 2>&1 && ok "twine check" || fail "twine check failed"
  if ( set -o pipefail; TESTING=True uv run pytest -q -m "not integration" >/dev/null 2>&1 ); then
    ok "unit tests (-m 'not integration')"
  else
    fail "unit tests failed (run: TESTING=True uv run pytest -q -m 'not integration')"
  fi
fi

echo "${GRN}✓ release preflight passed for $tag${RST}"
