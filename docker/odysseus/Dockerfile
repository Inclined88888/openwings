# Wings image = the official Odysseus image, pinned by digest, plus a small
# overlay of patched files (see patches/ for the human-readable diffs).
#
# Why an overlay on the published image instead of building upstream from
# source: the published image already carries the native toolchain (Chromium,
# Node, build tools), so a first install only downloads it and copies a few
# files. Building from source takes far longer and needs apt, npm and PyPI.
ARG ODYSSEUS_BASE_IMAGE=ghcr.io/odysseus-dev/odysseus:1.0.3-9d0d372@sha256:5117c5e07e5976095ce213e601277ff372e0387a67de34c5fd59a3de723e6113
FROM ${ODYSSEUS_BASE_IMAGE}

COPY overlay/ /tmp/wings/

# 1. Guard: every file we replace must still be byte-identical to the upstream
#    version the patches were written against. If the base image is bumped and
#    upstream changed one of these files, the build stops here instead of
#    silently reverting upstream fixes. Rebase with scripts/dev/rebase-overlay.sh.
# 2. Copy the overlay into /app.
# 3. Fail the build on a syntax error in any overlaid Python file.
RUN set -eu; \
    cd /app; \
    if [ -s /tmp/wings/UPSTREAM_SHA256SUMS ] && ! sha256sum --check --strict --quiet /tmp/wings/UPSTREAM_SHA256SUMS; then \
      echo "Wings overlay does not match this base image. Rebase the patches (scripts/dev/rebase-overlay.sh) before changing ODYSSEUS_BASE_IMAGE." >&2; \
      exit 1; \
    fi; \
    cp -R /tmp/wings/app/. /app/; \
    cd /tmp/wings/app; \
    find . -name '*.py' -print | sed 's|^\./|/app/|' | xargs -r python -m py_compile; \
    rm -rf /tmp/wings /app/.gitkeep

# Wings preflight (encryption-key checks) runs first, then the upstream
# entrypoint. Setting ENTRYPOINT clears the inherited CMD, so restate it.
ENTRYPOINT ["/app/wings/entrypoint.sh"]
# wings.serve = uvicorn, after moving ODYSSEUS_APP_KEY into memory and
# dropping ODYSSEUS_ADMIN_PASSWORD, so no child process inherits either.
CMD ["python", "-m", "wings.serve", "app:app", "--host", "0.0.0.0", "--port", "7000"]

LABEL org.opencontainers.image.title="wings-odysseus" \
      org.opencontainers.image.description="Odysseus with the Wings hardening overlay" \
      org.opencontainers.image.licenses="AGPL-3.0-or-later"
