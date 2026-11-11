#!/usr/bin/env bash
# Regenerate overlay/ from patches/ against the pinned upstream commit.
#
#   scripts/dev/rebase-overlay.sh                 use upstream.lock
#   ODYSSEUS_SRC=/path/to/odysseus scripts/dev/rebase-overlay.sh
#                                                 use an existing checkout
#
# patches/*.patch are the source of truth for every change to upstream files.
# This script applies them to a clean checkout of ODYSSEUS_COMMIT, copies each
# patched file into overlay/app/<path>, and records the SHA-256 of the
# unpatched upstream file in overlay/UPSTREAM_SHA256SUMS. The image build
# refuses to run if the base image's copy of any of those files differs, so a
# base-image bump without a rebase fails loudly instead of reverting fixes.
#
# To move to a new upstream release:
#   1. edit ODYSSEUS_COMMIT / ODYSSEUS_IMAGE / ODYSSEUS_IMAGE_DIGEST in
#      upstream.lock and the image reference in docker-compose.yml
#   2. run this script; fix any patch that no longer applies (git apply -3)
#   3. rebuild and run tests/e2e
#
# Files under overlay/app/wings/ are OpenWings-only additions and are left alone.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
# shellcheck disable=SC1091
. "$ROOT/upstream.lock"

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

if [ -n "${ODYSSEUS_SRC:-}" ]; then
  git -C "$ODYSSEUS_SRC" cat-file -e "${ODYSSEUS_COMMIT}^{commit}" \
    || { echo "commit ${ODYSSEUS_COMMIT} not found in ${ODYSSEUS_SRC}" >&2; exit 1; }
  git -C "$ODYSSEUS_SRC" worktree add --detach "$work/src" "$ODYSSEUS_COMMIT" >/dev/null
  cleanup_worktree() { git -C "$ODYSSEUS_SRC" worktree remove --force "$work/src" >/dev/null 2>&1 || true; rm -rf "$work"; }
  trap cleanup_worktree EXIT
else
  git init -q "$work/src"
  git -C "$work/src" fetch -q --depth 1 "$ODYSSEUS_REPO" "$ODYSSEUS_COMMIT"
  git -C "$work/src" checkout -q FETCH_HEAD
fi
src="$work/src"

shopt -s nullglob
patches=("$ROOT"/patches/*.patch)
[ ${#patches[@]} -gt 0 ] || { echo "no patches in $ROOT/patches" >&2; exit 1; }

# Files touched by any patch, relative to the upstream root.
files="$(for p in "${patches[@]}"; do git -C "$src" apply --numstat "$p" | awk '{print $3}'; done | sort -u)"
# Upstream's image leaves documentation and tests out (its .dockerignore
# drops docs/, website/, tests/ and *.md). Patches may update those files for
# anyone applying them to a source checkout, but they are not in the image,
# so they are neither checksummed nor overlaid (the checksum guard would fail
# on a missing file, and tests do not belong in the image).
files="$(printf '%s\n' "$files" | grep -Ev '^(docs|dev-docs|website|tests)/|\.md$' || true)"

# Hash the pristine upstream versions before patching.
sums="$work/UPSTREAM_SHA256SUMS"
: > "$sums"
for f in $files; do
  if [ -f "$src/$f" ]; then
    (cd "$src" && sha256sum "$f") >> "$sums"
  fi
done

for p in "${patches[@]}"; do
  echo "applying $(basename "$p")"
  git -C "$src" apply --whitespace=nowarn "$p"
done

# Replace the generated part of the overlay. A file a patch deletes cannot be
# overlaid; it is listed in overlay/UPSTREAM_DELETED and the image build
# removes it (its upstream checksum is still checked first).
find "$ROOT/overlay/app" -mindepth 1 -maxdepth 1 ! -name wings ! -name .gitkeep -exec rm -rf {} +
deleted="$work/UPSTREAM_DELETED"
: > "$deleted"
for f in $files; do
  if [ ! -e "$src/$f" ]; then
    grep -q "  $f\$" "$sums" && printf '%s\n' "$f" >> "$deleted"
    continue
  fi
  [ -f "$src/$f" ] || continue
  mkdir -p "$ROOT/overlay/app/$(dirname "$f")"
  cp "$src/$f" "$ROOT/overlay/app/$f"
done
sort -k2 "$sums" > "$ROOT/overlay/UPSTREAM_SHA256SUMS"
sort "$deleted" > "$ROOT/overlay/UPSTREAM_DELETED"

echo "overlay regenerated for ${ODYSSEUS_COMMIT}:"
sed 's/^/  /' "$ROOT/overlay/UPSTREAM_SHA256SUMS"
if [ -s "$ROOT/overlay/UPSTREAM_DELETED" ]; then
  echo "removed from the image:"
  sed 's/^/  /' "$ROOT/overlay/UPSTREAM_DELETED"
fi
