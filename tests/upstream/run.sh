#!/usr/bin/env bash
# Run upstream's own test suite inside the built OpenWings image (no network).
#
#   tests/upstream/run.sh /path/to/upstream/tests [pytest args]
#
# The image does not ship upstream's packaging files (see wings/image-tidy.sh),
# so the tests that only read those are left out (tests/upstream/not-shipped.txt).
# Compare the failures with the baseline in docs/verification.md.
set -euo pipefail
# shellcheck source=scripts/lib.sh
. "$(cd "$(dirname "$0")/../.." && pwd)/scripts/lib.sh"
[ -d "${1:-}" ] || die "usage: tests/upstream/run.sh /path/to/upstream/tests [pytest args]"
TESTS="$(cd "$1" && pwd)"; shift
require_docker
IMAGE="$(wings_image)"
skip=()
while IFS= read -r id; do
  case "$id" in ''|'#'*) continue ;; *::*) skip+=(--deselect "$id") ;; *) skip+=(--ignore "$id") ;; esac
done < "${WINGS_ROOT}/tests/upstream/not-shipped.txt"
docker run --rm --network none --entrypoint python -e HOME=/tmp -v "${TESTS}:/app/tests:ro" -w /app "$IMAGE" \
  -m pytest -q -p no:cacheprovider -rfE "${skip[@]}" tests "$@"
