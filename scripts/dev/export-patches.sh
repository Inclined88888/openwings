#!/usr/bin/env bash
# Regenerate patches/*.patch from an upstream checkout you have edited.
#
#   scripts/dev/export-patches.sh /path/to/odysseus-checkout
#
# The checkout must be at ODYSSEUS_COMMIT (upstream.lock) with all Wings
# changes applied in its working tree (new files added with `git add -N`).
# Each patch keeps its description header (everything up to and including the
# "License:" line) and gets a fresh diff of the files listed in PATCH_FILES.
# Then run scripts/dev/rebase-overlay.sh to refresh overlay/.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
SRC="${1:?usage: export-patches.sh /path/to/odysseus-checkout}"

PATCH_FILES=(
  "0001-encryption-key-from-env.patch|src/secret_storage.py src/api_key_manager.py"
  "0002-scrub-secrets-from-child-processes.patch|src/mcp_manager.py routes/cookbook_routes.py routes/shell_routes.py"
  "0003-encrypt-session-headers-and-mcp-env.patch|core/database.py routes/mcp/mcp_routes.py"
  "0004-email-send-requires-a-person.patch|routes/email mcp_servers/email_server.py"
  "0005-per-agent-tool-access.patch|src/request_models.py routes/preset_routes.py src/preset_manager.py routes/chat_routes.py routes/model_routes.py static/js/presets.js static/js/wings-agent-tools.js static/js/group.js"
  "0006-sidebar-agents-files-and-os-theme.patch|static/index.html static/js/theme.js static/js/wings-ui.js"
  "0007-safer-defaults-and-caldav-fix.patch|src/task_scheduler.py src/agent_loop.py services/skills/publication.py services/memory/skills.py static/js/memory.js src/caldav_writeback.py"
)

for entry in "${PATCH_FILES[@]}"; do
  name="${entry%%|*}"; files="${entry#*|}"
  patch="$ROOT/patches/$name"
  [ -f "$patch" ] || { echo "missing $patch" >&2; exit 1; }
  header="$(sed -n '1,/^License:/p' "$patch")"
  # shellcheck disable=SC2086  # $files is a space separated path list
  diff="$(git -C "$SRC" diff -- $files)"
  [ -n "$diff" ] || { echo "no changes for $name in $SRC" >&2; exit 1; }
  printf '%s\n\n%s\n' "$header" "$diff" > "$patch"
  echo "wrote $name"
done
