#!/usr/bin/env bash
# Regenerate patches/*.patch from an upstream checkout that carries the OpenWings
# changes as a patch series: one commit per patch on top of UPSTREAM_COMMIT
# (upstream.lock), each commit's subject being the patch name without
# ".patch" (for example "0008-browser-automation"). Patches may touch the
# same file; each one is the diff of its own commit, so they apply in order.
#
#   scripts/dev/export-patches.sh /path/to/upstream-checkout
#
# To change a patch: check out the series, edit, then `git commit --fixup`
# and `git rebase --autosquash UPSTREAM_COMMIT` (or amend the last commit).
# Each patch file keeps its description header (everything up to and
# including the "License:" line). Then run scripts/dev/rebase-overlay.sh to
# refresh overlay/.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
SRC="${1:?usage: export-patches.sh /path/to/upstream-checkout}"
# shellcheck disable=SC1091
. "$ROOT/upstream.lock"

if ! git -C "$SRC" diff --quiet || ! git -C "$SRC" diff --cached --quiet; then
  echo "$SRC has uncommitted changes; commit them into the series first" >&2
  exit 1
fi

count=0
while read -r sha subject; do
  patch="$ROOT/patches/${subject}.patch"
  [ -f "$patch" ] || { echo "commit $sha '$subject' has no patches/${subject}.patch (create it with a header ending in a License: line)" >&2; exit 1; }
  header="$(sed -n '1,/^License:/p' "$patch")"
  diff="$(git -C "$SRC" diff "${sha}^" "$sha")"
  [ -n "$diff" ] || { echo "commit $sha '$subject' is empty" >&2; exit 1; }
  printf '%s\n\n%s\n' "$header" "$diff" > "$patch"
  echo "wrote ${subject}.patch"
  count=$((count + 1))
done < <(git -C "$SRC" log --reverse --format='%H %s' "${UPSTREAM_COMMIT}..HEAD")

[ "$count" -gt 0 ] || { echo "no commits on top of ${UPSTREAM_COMMIT} in $SRC" >&2; exit 1; }
for patch in "$ROOT"/patches/*.patch; do
  name="$(basename "$patch" .patch)"
  # grep without -q reads all of git log's output, so pipefail never sees a SIGPIPE.
  git -C "$SRC" log --format='%s' "${UPSTREAM_COMMIT}..HEAD" | grep -x "$name" >/dev/null \
    || echo "warning: patches/${name}.patch has no matching commit in $SRC (stale?)" >&2
done
