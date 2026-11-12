#!/bin/bash
# Pack Hermes Agent memory and past chats for "Import from Hermes" in OpenWings.
# Reads only: memories/MEMORY.md, memories/USER.md, memory_store.db (facts table),
# state.db (sessions + user/assistant message text) and the user/assistant lines
# of legacy sessions/*.jsonl,
# for the default home and every profile. Never reads .env, auth.json, config.yaml,
# vault, mcp-tokens, pairing, browser profiles, skills or logs.
# Works with bash 3.2 (macOS) and Linux. Needs sqlite3 or python3 for chats.
set -u
umask 077
H="${HERMES_HOME:-$HOME/.hermes}"
if [ ! -d "$H" ]; then echo "No Hermes folder found at $H (set HERMES_HOME if you moved it)." >&2; exit 1; fi
DEST="$HOME/Desktop"; [ -d "$DEST" ] || DEST="$HOME"
OUT="$DEST/openwings-hermes-$(date +%Y%m%d-%H%M%S).tar.gz"
T="$(mktemp -d "${TMPDIR:-/tmp}/owhermes.XXXXXX")" || exit 1
trap 'rm -rf "$T"' EXIT
R="$T/openwings-hermes"; mkdir -p "$R"

SQ=""; command -v sqlite3 >/dev/null 2>&1 && SQ=sqlite3
# python3 only when sqlite3 is missing: on a Mac without the developer tools,
# /usr/bin/python3 is a stub that opens an install dialog.
PY=""
if [ -z "$SQ" ] && command -v python3 >/dev/null 2>&1 && python3 -c 'import sqlite3' >/dev/null 2>&1; then PY=python3; fi

# legacy IN OUT : keep only user and assistant lines of an old session file
# (tool output can hold keys or file contents, so it never leaves the Mac).
legacy() {
  if [ -n "$PY" ]; then
    python3 -I -c '
import json, sys
with open(sys.argv[1], encoding="utf-8", errors="replace") as src, open(sys.argv[2], "w", encoding="utf-8") as dst:
    for line in src:
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if isinstance(r, dict) and r.get("role") in ("user", "assistant") and isinstance(r.get("content"), str):
            dst.write(json.dumps({k: r.get(k) for k in ("role", "content", "timestamp")}) + "\n")' "$1" "$2"
  else
    grep -E '^[[:space:]]*\{.*"role"[[:space:]]*:[[:space:]]*"(user|assistant)"' "$1" \
      | grep -vE '"role"[[:space:]]*:[[:space:]]*"(tool|system|function)"' >"$2"
  fi
  [ -s "$2" ] || rm -f "$2"
}

# q DB OUTFILE SQL_NEW SQL_OLD : one JSON object per line; SQL_OLD is for older schemas.
q() {
  if [ -n "$SQ" ]; then
    sqlite3 -readonly -batch "$1" "$3" >"$2" 2>/dev/null || sqlite3 -readonly -batch "$1" "$4" >"$2" 2>/dev/null
  elif [ -n "$PY" ]; then
    python3 -I -c '
import sqlite3, sys, urllib.parse
db, out, sqls = sys.argv[1], sys.argv[2], sys.argv[3:]
con = sqlite3.connect("file:" + urllib.parse.quote(db) + "?mode=ro", uri=True)
for sql in sqls:
    try:
        rows = con.execute(sql).fetchall()
    except sqlite3.Error:
        continue
    with open(out, "w", encoding="utf-8") as f:
        for (line,) in rows:
            f.write(line + "\n")
    sys.exit(0)
sys.exit(1)' "$1" "$2" "$3" "$4"
  else
    return 2
  fi
}

S_NEW="SELECT json_object('id',id,'source',source,'title',title,'model',model,'parent_session_id',parent_session_id,'started_at',started_at,'ended_at',ended_at,'end_reason',end_reason,'archived',archived,'delegate',CASE WHEN json_valid(model_config) AND json_extract(model_config,'$._delegate_from') IS NOT NULL THEN 1 ELSE 0 END) FROM sessions ORDER BY started_at;"
S_OLD="SELECT json_object('id',id,'source',source,'title',NULL,'model',model,'parent_session_id',parent_session_id,'started_at',started_at,'ended_at',ended_at,'end_reason',end_reason) FROM sessions ORDER BY started_at;"
M_NEW="SELECT json_object('session_id',session_id,'role',role,'content',content,'timestamp',timestamp) FROM messages WHERE role IN ('user','assistant') AND content IS NOT NULL AND TRIM(content)<>'' AND (active=1 OR compacted=1) AND COALESCE(display_kind,'')<>'hidden' ORDER BY session_id, timestamp, id;"
M_OLD="SELECT json_object('session_id',session_id,'role',role,'content',content,'timestamp',timestamp) FROM messages WHERE role IN ('user','assistant') AND content IS NOT NULL AND TRIM(content)<>'' ORDER BY session_id, timestamp, id;"
F_SQL="SELECT json_object('content',content,'category',category,'tags',tags,'trust_score',trust_score,'created_at',created_at) FROM facts ORDER BY fact_id;"

NOCHATS=0; PROFILES=""
for P in "$H" "$H"/profiles/*; do
  [ -d "$P" ] || continue
  if [ "$P" = "$H" ]; then N="default"; else
    N="$(basename "$P" | tr '[:upper:]' '[:lower:]' | tr -cd 'a-z0-9_-' | cut -c1-64)"
    [ -n "$N" ] || N="p$(printf '%s' "$P" | cksum | cut -d' ' -f1)"
    N="profile-$N"
  fi
  D="$R/$N"; mkdir -p "$D"
  for f in MEMORY.md USER.md; do [ -f "$P/memories/$f" ] && cp "$P/memories/$f" "$D/$f"; done
  if [ -f "$P/state.db" ]; then
    if q "$P/state.db" "$D/sessions.jsonl" "$S_NEW" "$S_OLD"; then
      q "$P/state.db" "$D/messages.jsonl" "$M_NEW" "$M_OLD"
    else NOCHATS=1; fi
  fi
  [ -f "$P/memory_store.db" ] && { q "$P/memory_store.db" "$D/facts.jsonl" "$F_SQL" "$F_SQL" || rm -f "$D/facts.jsonl"; }
  for j in "$P"/sessions/*.jsonl; do
    [ -f "$j" ] || continue
    case "$(basename "$j")" in request_dump*|session_*) continue ;; esac
    mkdir -p "$D/legacy"; legacy "$j" "$D/legacy/$(basename "$j")"
  done
  [ -d "$D/legacy" ] && rmdir "$D/legacy" 2>/dev/null
  if [ -z "$(ls -A "$D")" ]; then rmdir "$D"; else PROFILES="$PROFILES\"$N\","; fi
done
[ -n "$PROFILES" ] || { echo "Found $H but no Hermes memory or chats in it." >&2; exit 1; }
printf '{"format":"openwings-hermes-pack","version":1,"created_at":"%s","profiles":[%s]}\n' \
  "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "${PROFILES%,}" >"$R/manifest.json"
tar -czf "$OUT" -C "$T" openwings-hermes || exit 1
echo "Saved $OUT"
[ "$NOCHATS" = 1 ] && echo "Note: past chats could not be read (this needs sqlite3 or python3). Memories are included."
echo "Now open OpenWings, choose Memory in the side menu, then Import from Hermes, and choose this file."
