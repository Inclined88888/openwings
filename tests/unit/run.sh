#!/usr/bin/env bash
# Run the OpenWings unit tests inside the built image (no network, no running app).
set -euo pipefail
# shellcheck source=scripts/lib.sh
. "$(cd "$(dirname "$0")/../.." && pwd)/scripts/lib.sh"
require_docker
IMAGE="$(wings_image)"
docker run --rm --network none --entrypoint python -e HOME=/tmp \
  -v "${WINGS_ROOT}/tests/unit:/wings-tests:ro" -v "${WINGS_ROOT}/docker/runner:/wings-runner:ro" -w /app "$IMAGE" \
  -m pytest -q -p no:cacheprovider -o cache_dir=/tmp/pc --rootdir=/wings-tests /wings-tests "$@"
