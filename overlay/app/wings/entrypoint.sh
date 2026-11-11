#!/bin/sh
# OpenWings entrypoint: run the preflight checks, then hand over to the upstream
# Odysseus entrypoint (which fixes volume ownership and drops privileges).
set -e
python /app/wings/preflight.py
exec /usr/local/bin/entrypoint.sh "$@"
