"""Import from Hermes: bring Hermes Agent memories and past chats into OpenWings.

Hermes Agent (Nous Research) keeps its data in the person's home folder
(~/.hermes, every profile under ~/.hermes/profiles/<name>). OpenWings runs in
a container and cannot read that folder, so the person runs one line in a
Terminal (static/openwings/hermes-pack.sh) that packs only the memory files
and the user/assistant text of past chats into one archive, then uploads it.

Layout of the archive (what hermes-pack.sh writes):

    openwings-hermes/manifest.json
    openwings-hermes/<default|profile-NAME>/MEMORY.md     entries split by "\\n§\\n"
    openwings-hermes/<default|profile-NAME>/USER.md
    openwings-hermes/<default|profile-NAME>/facts.jsonl   Holographic memory facts
    openwings-hermes/<default|profile-NAME>/sessions.jsonl
    openwings-hermes/<default|profile-NAME>/messages.jsonl
    openwings-hermes/<default|profile-NAME>/legacy/*.jsonl

Also accepted: a tar of the Hermes folder itself (only memories/*.md and
sessions/*.jsonl are read from it), a bare MEMORY.md or USER.md, a
`hermes sessions export` JSONL file, and pasted text.

Everything else in an upload is ignored and named in the preview (.env,
auth.json, config.yaml, state.db, skills, SOUL.md...). Imported text is data:
secrets are scrubbed, entries that read like instructions are flagged and
left unticked, memories are never pinned and reach the model only through the
existing untrusted memory context, and imported chats are read only, with
memory extraction and injection off.

Nothing here imports the upstream `src` package at module load time.
"""
from __future__ import annotations

import hashlib
import io
import json
import logging
import re
import secrets
import tarfile
import threading
import time
import uuid
import zipfile
import zlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

logger = logging.getLogger("wings.hermes_import")

MAX_UPLOAD = 50 * 1024 * 1024
MAX_MEMBERS = 5000
MAX_TOTAL = 200 * 1024 * 1024
MAX_FILE = 64 * 1024 * 1024
# Every member of a tar is decompressed while reading past it, wanted or
# not, so all of them count toward this (a bomb stops here, not at MAX_TOTAL).
MAX_SCANNED = 1024 * 1024 * 1024
MAX_PENDING = 32
MAX_MEMORY_TEXT = 5000
MAX_MESSAGE_TEXT = 100_000
MAX_CHATS = 5000
TOKEN_TTL = 30 * 60
FOLDER = "From Hermes"
SOURCE = "hermes_import"
READ_ONLY_MESSAGE = ("This chat was imported from Hermes and is read only. "
                     "Start a new chat to continue it.")

ENTRY_DELIMITER = re.compile(r"\n\s*§\s*\n")
_PROFILE_RE = re.compile(r"^(default|profile-[a-z0-9_-]{1,64})$")
_SKIP_SOURCES = {"subagent", "cron", "kanban"}
_SUMMARY_PREFIXES = ("[CONTEXT COMPACTION", "[CONTEXT SUMMARY]:")


class ImportProblem(ValueError):
    """A plain sentence for the person."""


# ------------------------------------------------------------------ safety

_SECRET_PATTERNS = [
    re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?(?:-----END [A-Z0-9 ]*PRIVATE KEY-----|\Z)", re.S),
    re.compile(r"\b(?:sk|rk|pk)-(?:or-|ant-|proj-|live-|test-)?[A-Za-z0-9_\-]{16,}"),
    re.compile(r"\bxai-[A-Za-z0-9]{20,}"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_\-]{30,}"),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9\-]{10,}"),
    re.compile(r"\bhf_[A-Za-z0-9]{20,}"),
    re.compile(r"\bBearer\s+[A-Za-z0-9._\-~+/]{16,}=*", re.I),
    re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}"),
]
_SECRET_ASSIGN = re.compile(
    r"(?im)^(\s*(?:export\s+)?[A-Za-z_][A-Za-z0-9_]*(?:KEY|SECRET|TOKEN|PASSWORD|PASSWD|CREDENTIAL|PRIVATE)"
    r"[A-Za-z0-9_]*\s*[=:]\s*)(\S.*)$"
)
_SECRET_PHRASE = re.compile(
    r"(?i)\b((?:api[ _-]?key|password|passwd|secret|token|passphrase)\s*(?:is|=|:)\s*)([^\s,;]{6,})"
)
REMOVED = "[removed]"


def scrub_secrets(text: str) -> Tuple[str, int]:
    """Replace things that look like keys, tokens or passwords."""
    count = 0
    out = str(text or "")
    for pat in _SECRET_PATTERNS:
        out, n = pat.subn(REMOVED, out)
        count += n
    out, n = _SECRET_ASSIGN.subn(lambda m: m.group(1) + REMOVED, out)
    count += n

    def phrase(m):
        if m.group(2) == REMOVED:
            return m.group(0)
        return m.group(1) + REMOVED
    out, n = _SECRET_PHRASE.subn(phrase, out)
    count += n
    return out, count


_INSTRUCTION_PATTERNS = [
    (re.compile(r"(?i)\b(?:ignore|disregard|forget|override)\b.{0,40}\b(?:previous|prior|above|earlier|all|your|system)\b.{0,30}\b(?:instructions?|rules?|prompts?|messages?|guidelines?)\b"), "tells the assistant to ignore its rules"),
    (re.compile(r"(?i)\b(?:system prompt|developer message|jailbreak)\b"), "talks about the assistant's instructions"),
    (re.compile(r"(?i)\byou are now\b|\bact as\b|\bfrom now on,? (?:you|always|never)\b|\bnew instructions?\b"), "tries to change how the assistant behaves"),
    (re.compile(r"(?i)\b(?:always|never|must)\b.{0,40}\b(?:call|run|execute|use)\b.{0,30}\b(?:tool|function|command|bash|shell|python)\b"), "tells the assistant to run tools"),
    (re.compile(r"(?i)\b(?:send|forward|email|post|upload|exfiltrate)\b.{0,60}\b(?:password|passwords|keys?|tokens?|secrets?|credentials|memories|files)\b.{0,40}\b(?:to|at)\b"), "asks to send private data somewhere"),
    (re.compile(r"<<<\s*/?\s*UNTRUSTED|<\|im_start\|>|<\|system\|>|\[/?INST\]|</?tool_call>|```(?:bash|sh|python|tool)"), "contains prompt or tool syntax"),
    (re.compile(r"(?i)https?://[^\s]+\?[^\s]*(?:key|token|secret|data|q)=\S*"), "has a link that sends data"),
]


def instruction_flag(text: str) -> str:
    """A short reason when the text reads like instructions to an assistant."""
    for pat, reason in _INSTRUCTION_PATTERNS:
        if pat.search(text or ""):
            return reason
    return ""


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip().casefold()


def _decode(data: bytes) -> str:
    return data.decode("utf-8-sig", errors="replace")


def _jsonl(data: bytes) -> Iterable[Dict[str, Any]]:
    for line in _decode(data).splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except (ValueError, RecursionError):
            continue
        if isinstance(row, dict):
            yield row


def _ts(value: Any) -> Optional[float]:
    """Unix seconds from an epoch number or an ISO time (local naive times are taken as UTC)."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        v = float(value)
        if v > 1e12:          # milliseconds
            v /= 1000.0
        return v if 0 < v < 4e9 else None
    s = str(value).strip()
    try:
        return _ts(float(s))
    except ValueError:
        pass
    try:
        d = datetime.fromisoformat(s.replace("Z", "+00:00").replace(" ", "T", 1))
    except ValueError:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.timestamp()


# ------------------------------------------------------------------ reading an upload

@dataclass
class Profile:
    name: str
    memory_md: Optional[bytes] = None
    user_md: Optional[bytes] = None
    facts: Optional[bytes] = None
    sessions: Optional[bytes] = None
    messages: Optional[bytes] = None
    legacy: Dict[str, bytes] = field(default_factory=dict)
    exports: List[bytes] = field(default_factory=list)


@dataclass
class Upload:
    profiles: Dict[str, Profile] = field(default_factory=dict)
    ignored: List[str] = field(default_factory=list)

    def profile(self, name: str) -> Profile:
        return self.profiles.setdefault(name, Profile(name))


def _safe_member_name(name: str) -> Optional[str]:
    n = name.replace("\\", "/")
    while n.startswith("./"):
        n = n[2:]
    if not n or n.startswith("/") or re.match(r"^[A-Za-z]:", n):
        return None
    parts = [p for p in n.split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts):
        return None
    return "/".join(parts)


def _place(up: Upload, path: str, data: bytes) -> None:
    """Route one archive member to its slot, or record it as ignored."""
    parts = path.split("/")
    # Our pack: openwings-hermes/<profile>/<file>
    if parts[0] == "openwings-hermes":
        rest = parts[1:]
        if rest == ["manifest.json"]:
            return
        if len(rest) >= 2 and _PROFILE_RE.match(rest[0]):
            prof, tail = rest[0], rest[1:]
            p = up.profile(prof)
            if tail == ["MEMORY.md"]:
                p.memory_md = data
                return
            if tail == ["USER.md"]:
                p.user_md = data
                return
            if tail == ["facts.jsonl"]:
                p.facts = data
                return
            if tail == ["sessions.jsonl"]:
                p.sessions = data
                return
            if tail == ["messages.jsonl"]:
                p.messages = data
                return
            if len(tail) == 2 and tail[0] == "legacy" and tail[1].endswith(".jsonl"):
                p.legacy[tail[1]] = data
                return
        up.ignored.append(path)
        return
    # A tar of the Hermes folder itself (or the memories only one liner):
    # [.hermes/][profiles/<name>/]memories/{MEMORY,USER}.md and sessions/<id>.jsonl
    rest = parts[1:] if parts[0] in (".hermes", "hermes") else parts
    prof = "default"
    if len(rest) >= 3 and rest[0] == "profiles":
        clean = re.sub(r"[^a-z0-9_-]", "", rest[1].lower())[:64]
        if clean:
            prof, rest = f"profile-{clean}", rest[2:]
    if rest == ["memories", "MEMORY.md"]:
        up.profile(prof).memory_md = data
        return
    if rest == ["memories", "USER.md"]:
        up.profile(prof).user_md = data
        return
    if (len(rest) == 2 and rest[0] == "sessions" and rest[1].endswith(".jsonl")
            and not rest[1].startswith(("request_dump", "session_"))):
        up.profile(prof).legacy[rest[1]] = data
        return
    up.ignored.append(path)


def read_upload(filename: str, data: bytes) -> Upload:
    """Parse an uploaded archive or file in memory. Nothing touches the disk."""
    if len(data) > MAX_UPLOAD:
        raise ImportProblem("That file is larger than 50 MB. Pack only the Hermes memory and chats with the command shown.")
    up = Upload()
    name = (filename or "").lower()
    head = data[:4]
    if head[:3] == b"BZh" or head[:4] == b"\xfd7zX" or data[:6] == b"\xfd7zXZ\x00":
        raise ImportProblem("Only .tar.gz, .tar and .zip archives can be read. Upload the file the command saved on your Desktop.")
    if head[:2] == b"\x1f\x8b" or name.endswith((".tar.gz", ".tgz", ".tar")) or _looks_tar(data):
        # gzip or plain tar only: xz and bz2 pack gigabytes into a few KB.
        mode = "r:gz" if head[:2] == b"\x1f\x8b" else "r:"
        bad = ImportProblem("This file could not be opened as an archive. Upload the file the command saved on your Desktop.")
        try:
            return _read_tar(up, tarfile.open(fileobj=io.BytesIO(data), mode=mode))
        except ImportProblem:
            raise
        except (tarfile.TarError, EOFError, OSError, zlib.error, ValueError, RecursionError):
            raise bad
    if head == b"PK\x03\x04":
        return _read_zip(up, data)
    return _read_single(up, name, data)


def _read_tar(up: Upload, tf: tarfile.TarFile) -> Upload:
    total = 0
    scanned = 0
    with tf:
        for i, m in enumerate(tf):
            if i >= MAX_MEMBERS:
                raise ImportProblem("This archive has too many files. Use the command shown, which packs only what is needed.")
            scanned += max(0, int(m.size or 0)) + 512
            if scanned > MAX_SCANNED:
                raise ImportProblem("This archive is too large once unpacked. Use the command shown, which packs only what is needed.")
            safe = _safe_member_name(m.name)
            if m.isdir():
                continue
            if not m.isfile() or safe is None:
                up.ignored.append(m.name)
                continue
            if not _wanted(safe) or m.size > MAX_FILE:
                up.ignored.append(safe)
                continue
            total += m.size
            if total > MAX_TOTAL:
                raise ImportProblem("This archive is too large once unpacked.")
            fh = tf.extractfile(m)
            if fh is None:
                continue
            _place(up, safe, fh.read(MAX_FILE + 1)[:MAX_FILE])
    return up


def _read_zip(up: Upload, data: bytes) -> Upload:
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except (zipfile.BadZipFile, ValueError, OSError):
        raise ImportProblem("This zip file could not be opened.")
    total = 0
    try:
        with zf:
            for i, info in enumerate(zf.infolist()):
                if i >= MAX_MEMBERS:
                    raise ImportProblem("This archive has too many files.")
                if info.is_dir():
                    continue
                safe = _safe_member_name(info.filename)
                if safe is None or not _wanted(safe) or info.file_size > MAX_FILE:
                    up.ignored.append(info.filename)
                    continue
                total += info.file_size
                if total > MAX_TOTAL:
                    raise ImportProblem("This archive is too large once unpacked.")
                with zf.open(info) as fh:
                    _place(up, safe, fh.read(MAX_FILE + 1)[:MAX_FILE])
    except ImportProblem:
        raise
    except (zipfile.BadZipFile, zlib.error, EOFError, OSError, ValueError, NotImplementedError, RuntimeError):
        raise ImportProblem("This zip file could not be read. Upload the file the command saved on your Desktop.")
    return up


def _read_single(up: Upload, name: str, data: bytes) -> Upload:
    base = name.rsplit("/", 1)[-1]
    p = up.profile("default")
    if base == "user.md":
        p.user_md = data
    elif base.endswith((".md", ".txt")):
        p.memory_md = data
    elif base.endswith((".jsonl", ".json")):
        rows = list(_jsonl(data))
        if rows and isinstance(rows[0].get("messages"), list):
            p.exports.append(data)
        elif rows and "role" in rows[0]:
            p.legacy[base if base.endswith(".jsonl") else base + "l"] = data
        else:
            raise ImportProblem("This file does not look like Hermes chats or memories.")
    else:
        raise ImportProblem("Upload the .tar.gz file the command saved, or a MEMORY.md or USER.md file.")
    return up


def _looks_tar(data: bytes) -> bool:
    return len(data) > 262 and data[257:262] == b"ustar"


def _wanted(path: str) -> bool:
    """Only the allowlisted files are ever read; everything else is ignored unread."""
    base = path.rsplit("/", 1)[-1]
    if base in ("MEMORY.md", "USER.md", "facts.jsonl", "sessions.jsonl", "messages.jsonl", "manifest.json"):
        return True
    return base.endswith(".jsonl") and ("/legacy/" in path or "/sessions/" in "/" + path)


# ------------------------------------------------------------------ candidates

@dataclass
class MemoryItem:
    key: str
    text: str
    category: str
    profile: str
    file: str
    flag: str = ""
    duplicate: bool = False
    secrets_removed: int = 0
    low_trust: bool = False
    ts: Optional[float] = None

    def public(self) -> Dict[str, Any]:
        return {"key": self.key, "text": self.text, "category": self.category, "profile": self.profile,
                "file": self.file, "flag": self.flag, "duplicate": self.duplicate,
                "secrets_removed": self.secrets_removed,
                "selected": not (self.flag or self.duplicate or self.low_trust)}


@dataclass
class ChatItem:
    key: str
    title: str
    profile: str
    hermes_id: str
    model: str
    messages: List[Dict[str, Any]]
    started: float
    ended: float
    already: bool = False
    secrets_removed: int = 0

    def public(self) -> Dict[str, Any]:
        return {"key": self.key, "title": self.title, "profile": self.profile,
                "started": _iso(self.started), "ended": _iso(self.ended),
                "messages": len(self.messages), "already": self.already,
                "secrets_removed": self.secrets_removed, "selected": not self.already}


def _iso(ts: Optional[float]) -> Optional[str]:
    if not ts:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _memory_key(text: str) -> str:
    return hashlib.sha256(_norm(text).encode()).hexdigest()[:24]


def _chat_key(profile: str, hermes_id: str) -> str:
    return hashlib.sha256(f"{profile}\x00{hermes_id}".encode()).hexdigest()[:32]


def _split_entries(data: bytes) -> List[str]:
    raw = _decode(data).replace("\r\n", "\n")
    return [e.strip() for e in ENTRY_DELIMITER.split("\n" + raw + "\n") if e.strip()]


def _memory_items(up: Upload) -> List[MemoryItem]:
    items: List[MemoryItem] = []
    seen: set = set()

    def add(text: str, category: str, profile: str, file: str, low_trust: bool = False,
            ts: Optional[float] = None):
        text, removed = scrub_secrets(text.strip())
        text = text[:MAX_MEMORY_TEXT].strip()
        if not text or text == REMOVED:
            return
        key = _memory_key(text)
        if key in seen:
            return
        seen.add(key)
        items.append(MemoryItem(key=key, text=text, category=category, profile=profile, file=file,
                                flag=instruction_flag(text), secrets_removed=removed, low_trust=low_trust,
                                ts=ts))

    for prof in sorted(up.profiles):
        p = up.profiles[prof]
        if p.user_md:
            for e in _split_entries(p.user_md):
                add(e, "preference", prof, "USER.md")
        if p.memory_md:
            for e in _split_entries(p.memory_md):
                add(e, "fact", prof, "MEMORY.md")
        if p.facts:
            for row in _jsonl(p.facts):
                content = row.get("content")
                if not isinstance(content, str):
                    continue
                cat = str(row.get("category") or "").lower()
                cat = cat if cat in ("preference", "project", "contact", "goal", "task", "identity") else "fact"
                try:
                    trust = float(row.get("trust_score")) if row.get("trust_score") is not None else 0.5
                except (TypeError, ValueError):
                    trust = 0.5
                add(content, cat, prof, "facts", low_trust=trust < 0.3, ts=_ts(row.get("created_at")))
    return items


def _clean_message(role: Any, content: Any) -> Optional[Tuple[str, str]]:
    if role not in ("user", "assistant") or not isinstance(content, str):
        return None
    text = content.strip()
    if not text or text.startswith(_SUMMARY_PREFIXES):
        return None
    return role, text[:MAX_MESSAGE_TEXT]


def _title(title: Any, messages: List[Dict[str, Any]]) -> str:
    t = re.sub(r"\s+", " ", str(title or "")).strip()
    if not t:
        first = next((m["content"] for m in messages if m["role"] == "user"), "")
        t = re.sub(r"\s+", " ", first).strip()
    t = t[:60].rstrip() or "Hermes chat"
    return t


def _chat_items(up: Upload, include_background: bool = False) -> Tuple[List[ChatItem], int]:
    chats: List[ChatItem] = []
    skipped = 0
    for prof in sorted(up.profiles):
        p = up.profiles[prof]
        sessions: Dict[str, Dict[str, Any]] = {}
        if p.sessions:
            for row in _jsonl(p.sessions):
                sid = str(row.get("id") or "")
                if sid:
                    sessions[sid] = row
        by_session: Dict[str, List[Tuple[float, str, str]]] = {}
        if p.messages:
            for row in _jsonl(p.messages):
                msg = _clean_message(row.get("role"), row.get("content"))
                sid = str(row.get("session_id") or "")
                if not msg or not sid:
                    continue
                by_session.setdefault(sid, []).append((_ts(row.get("timestamp")) or 0.0, msg[0], msg[1]))
        for blob in p.exports:
            for row in _jsonl(blob):
                sid = str(row.get("id") or row.get("session_id") or "")
                if not sid or not isinstance(row.get("messages"), list):
                    continue
                meta = {k: row.get(k) for k in ("source", "title", "model", "parent_session_id",
                                                 "started_at", "end_reason")}
                if isinstance(row.get("model_config"), dict) and row["model_config"].get("_delegate_from"):
                    meta["delegate"] = 1
                sessions.setdefault(sid, {"id": sid, **meta})
                for m in row["messages"]:
                    if not isinstance(m, dict):
                        continue
                    if m.get("active") == 0 and not m.get("compacted"):
                        continue
                    if m.get("display_kind") == "hidden":
                        continue
                    msg = _clean_message(m.get("role"), m.get("content"))
                    if msg:
                        by_session.setdefault(sid, []).append((_ts(m.get("timestamp")) or 0.0, msg[0], msg[1]))
        # Compression children continue their parent's conversation.
        def root(sid: str) -> str:
            seen = set()
            while sid not in seen:
                seen.add(sid)
                parent = str((sessions.get(sid) or {}).get("parent_session_id") or "")
                if parent and (sessions.get(parent) or {}).get("end_reason") == "compression":
                    sid = parent
                    continue
                break
            return sid
        merged: Dict[str, List[Tuple[float, str, str]]] = {}
        for sid, rows in by_session.items():
            merged.setdefault(root(sid), []).extend(rows)
        for name, data in sorted(p.legacy.items()):
            sid = "legacy:" + name[:-6]
            rows = []
            for row in _jsonl(data):
                msg = _clean_message(row.get("role"), row.get("content"))
                if msg:
                    rows.append((_ts(row.get("timestamp")) or 0.0, msg[0], msg[1]))
            if rows and sid[7:] not in merged:
                merged[sid] = rows
        for sid, rows in merged.items():
            meta = sessions.get(sid) or {}
            source = str(meta.get("source") or "").lower()
            if not include_background and (source in _SKIP_SOURCES or meta.get("delegate") in (1, True, "1")):
                skipped += 1
                continue
            seen = set()
            msgs: List[Dict[str, Any]] = []
            removed_total = 0
            for ts, role, content in sorted(rows, key=lambda r: r[0]):
                k = (role, content, ts)
                if k in seen:
                    continue
                seen.add(k)
                text, removed = scrub_secrets(content)
                removed_total += removed
                msgs.append({"role": role, "content": text, "ts": ts})
            if not msgs:
                continue
            started = msgs[0]["ts"] or _ts(meta.get("started_at")) or time.time()
            ended = msgs[-1]["ts"] or started
            chats.append(ChatItem(
                key=_chat_key(prof, sid),
                title=_title(meta.get("title"), msgs), profile=prof, hermes_id=sid,
                model=re.sub(r"[^\w.:/\-]", "", str(meta.get("model") or ""))[:120] or "hermes",
                messages=msgs, started=started, ended=ended, secrets_removed=removed_total))
            if len(chats) >= MAX_CHATS:
                break
    chats.sort(key=lambda c: c.started)
    return chats, skipped


def _paste_items(text: str) -> Tuple[List[MemoryItem], List[ChatItem]]:
    """Pasted text: a transcript becomes one chat, anything else candidate memories."""
    raw = str(text or "").replace("\r\n", "\n").strip()
    if not raw:
        raise ImportProblem("Paste some text first.")
    if len(raw) > 2_000_000:
        raise ImportProblem("That is too much text to paste. Use the command and upload the file instead.")
    turn = re.compile(r"^\s*(you|user|me|human|ai|assistant|hermes|bot)\s*:\s?(.*)$", re.I)
    lines = raw.split("\n")
    turns: List[Tuple[str, str]] = []
    for line in lines:
        m = turn.match(line)
        if m:
            role = "user" if m.group(1).lower() in ("you", "user", "me", "human") else "assistant"
            turns.append((role, m.group(2)))
        elif turns:
            turns[-1] = (turns[-1][0], turns[-1][1] + "\n" + line)
    roles = {r for r, _ in turns}
    if len(turns) >= 2 and roles == {"user", "assistant"}:
        now = time.time()
        msgs = []
        removed_total = 0
        for i, (role, content) in enumerate(turns):
            content = content.strip()
            if not content:
                continue
            content, removed = scrub_secrets(content)
            removed_total += removed
            msgs.append({"role": role, "content": content[:MAX_MESSAGE_TEXT], "ts": now + i * 0.001})
        sid = "paste:" + hashlib.sha256(raw.encode()).hexdigest()[:16]
        return [], [ChatItem(key=_chat_key("pasted", sid), title="Pasted from Hermes", profile="pasted",
                             hermes_id=sid, model="hermes", messages=msgs, started=now, ended=now,
                             secrets_removed=removed_total)]
    up = Upload()
    if "§" in raw:
        up.profile("pasted").memory_md = raw.encode()
        return _memory_items(up), []
    parts = re.split(r"\n\s*\n|\n(?=\s*(?:[-*•]|\d{1,3}[.)])\s+)", raw)
    cleaned = []
    for part in parts:
        part = re.sub(r"^\s*(?:[-*•]|\d{1,3}[.)])\s+", "", part.strip())
        if part:
            cleaned.append(part)
    up.profile("pasted").memory_md = "\n§\n".join(cleaned).encode()
    return _memory_items(up), []


# ------------------------------------------------------------------ storage (OpenWings tables)

def _batch_model():
    from wings.accounts import register_model

    def make():
        from sqlalchemy import Column, DateTime, String, Text
        from core.database import Base, utcnow_naive

        class WingsHermesImport(Base):
            __tablename__ = "wings_hermes_imports"
            __table_args__ = {"extend_existing": True}
            id = Column(String, primary_key=True)
            owner = Column(String, nullable=False, index=True)
            created_at = Column(DateTime, default=utcnow_naive)
            memory_ids = Column(Text, nullable=False, default="[]")
            counts = Column(Text, nullable=False, default="{}")
        return WingsHermesImport
    return register_model("hermes_import", make)


def _chat_model():
    from wings.accounts import register_model

    def make():
        from sqlalchemy import Column, String
        from core.database import Base

        class WingsHermesChat(Base):
            __tablename__ = "wings_hermes_chats"
            __table_args__ = {"extend_existing": True}
            session_id = Column(String, primary_key=True)
            owner = Column(String, nullable=False, index=True)
            batch_id = Column(String, nullable=False, index=True)
            chat_key = Column(String, nullable=False, index=True)
        return WingsHermesChat
    return register_model("hermes_chat", make)


_ro_lock = threading.Lock()
_ro_cache: Optional[set] = None


def _invalidate_read_only() -> None:
    global _ro_cache
    with _ro_lock:
        _ro_cache = None


def is_read_only(session_id: Optional[str], owner: Optional[str] = None) -> bool:
    """True for a chat imported from Hermes (they stay as they were).

    With ``owner``, only that person's imported chats count, so the answer
    says nothing about other people's chats.
    """
    global _ro_cache
    if not session_id:
        return False
    with _ro_lock:
        cache = _ro_cache
    if cache is None:
        try:
            from wings.accounts import _db
            Chat = _chat_model()
            db = _db()
            try:
                cache = {row[0]: row[1] for row in db.query(Chat.session_id, Chat.owner).all()}
            finally:
                db.close()
        except Exception:
            logger.debug("read only lookup failed", exc_info=True)
            return False
        with _ro_lock:
            _ro_cache = cache
    sid = str(session_id)
    if sid not in cache:
        return False
    return owner is None or cache[sid] == owner


def refuse_if_read_only(session_id: Optional[str]) -> None:
    """Upstream hook: changing an imported chat answers 409 with a plain sentence."""
    if is_read_only(session_id):
        from fastapi import HTTPException
        raise HTTPException(409, READ_ONLY_MESSAGE)


def _imported_chat_keys(owner: str) -> set:
    """Hermes chats this person has in OpenWings now. A chat they deleted
    no longer counts, so importing the file again brings it back."""
    try:
        from wings.accounts import _db
        from core.database import Session as DbSession
        Chat = _chat_model()
        db = _db()
        try:
            rows = (db.query(Chat.chat_key)
                    .join(DbSession, DbSession.id == Chat.session_id)
                    .filter(Chat.owner == owner).all())
            return {row[0] for row in rows}
        finally:
            db.close()
    except Exception:
        logger.debug("imported chat lookup failed", exc_info=True)
        return set()


# ------------------------------------------------------------------ preview, import, undo

@dataclass
class Pending:
    owner: str
    memories: List[MemoryItem]
    chats: List[ChatItem]
    expires: float


_pending: Dict[str, Pending] = {}
_pending_lock = threading.Lock()


def _prune(now: Optional[float] = None) -> None:
    now = now or time.time()
    with _pending_lock:
        for k in [k for k, v in _pending.items() if v.expires < now]:
            _pending.pop(k, None)


def preview(owner: str, existing_memories: List[Dict[str, Any]], *, upload: Optional[Upload] = None,
            pasted: Optional[str] = None, include_background: bool = False) -> Dict[str, Any]:
    """Parse and count. Nothing is written; the result holds a token for import."""
    if not owner:
        raise ImportProblem("Sign in first.")
    skipped = 0
    ignored: List[str] = []
    if pasted is not None:
        memories, chats = _paste_items(pasted)
    elif upload is not None:
        memories = _memory_items(upload)
        chats, skipped = _chat_items(upload, include_background=include_background)
        ignored = upload.ignored
    else:
        raise ImportProblem("Upload a file or paste text.")
    mine = {_norm(m.get("text", "")) for m in existing_memories if m.get("owner") == owner}
    for m in memories:
        m.duplicate = _norm(m.text) in mine
    done = _imported_chat_keys(owner)
    for c in chats:
        c.already = c.key in done
    if not memories and not chats:
        raise ImportProblem("No Hermes memories or chats were found in that file.")
    _prune()
    token = secrets.token_urlsafe(24)
    with _pending_lock:
        # One open preview per person (a new one replaces it), and a cap on
        # all of them, so repeated previews cannot pile up parsed uploads.
        for k in [k for k, v in _pending.items() if v.owner == owner]:
            _pending.pop(k, None)
        while len(_pending) >= MAX_PENDING:
            oldest = min(_pending, key=lambda k: _pending[k].expires)
            _pending.pop(oldest, None)
        _pending[token] = Pending(owner=owner, memories=memories, chats=chats, expires=time.time() + TOKEN_TTL)
    dates = [c.started for c in chats] + [c.ended for c in chats]
    profiles = sorted({m.profile for m in memories} | {c.profile for c in chats})
    return {
        "token": token,
        "profiles": profiles,
        "memories": [m.public() for m in memories],
        "chats": [c.public() for c in chats],
        "counts": {
            "memories": len(memories),
            "memories_new": sum(1 for m in memories if not m.duplicate),
            "memories_duplicate": sum(1 for m in memories if m.duplicate),
            "memories_flagged": sum(1 for m in memories if m.flag),
            "chats": len(chats),
            "chats_new": sum(1 for c in chats if not c.already),
            "chats_already": sum(1 for c in chats if c.already),
            "chats_skipped_background": skipped,
            "messages": sum(len(c.messages) for c in chats),
            "secrets_removed": sum(m.secrets_removed for m in memories) + sum(c.secrets_removed for c in chats),
            "ignored_files": len(ignored),
        },
        "ignored": sorted(set(ignored))[:50],
        "first": _iso(min(dates)) if dates else None,
        "last": _iso(max(dates)) if dates else None,
    }


def _memory_vector_add(memory_vector, mid: str, text: str) -> None:
    try:
        if memory_vector is not None and getattr(memory_vector, "healthy", False):
            memory_vector.add(mid, text)
    except Exception:
        logger.debug("memory vector add failed", exc_info=True)


def _memory_vector_remove(memory_vector, mid: str) -> None:
    try:
        if memory_vector is not None and getattr(memory_vector, "healthy", False):
            memory_vector.remove(mid)
    except Exception:
        logger.debug("memory vector remove failed", exc_info=True)


def apply(owner: str, token: str, memory_keys: Iterable[str], chat_keys: Iterable[str], *,
          memory_manager, memory_vector=None, endpoint_url: str = "") -> Dict[str, Any]:
    """Write the chosen items for this person only, as one undoable batch."""
    _prune()
    with _pending_lock:
        pend = _pending.get(token)
        if pend is not None and pend.owner == owner:
            # Taken now: a second submit of the same preview finds nothing.
            _pending.pop(token, None)
    if pend is None or pend.owner != owner:
        raise ImportProblem("This preview has expired. Choose the file again.")
    want_m = set(memory_keys or [])
    want_c = set(chat_keys or [])
    batch_id = uuid.uuid4().hex
    now_s = int(time.time())

    # Memories: one read, one atomic save, then the vector index.
    added: List[Dict[str, Any]] = []
    if want_m:
        all_mem = memory_manager.load_all_for_update()
        mine = {_norm(m.get("text", "")) for m in all_mem if m.get("owner") == owner}
        for item in pend.memories:
            if item.key not in want_m or _norm(item.text) in mine:
                continue
            entry = memory_manager.add_entry(item.text, SOURCE, item.category, owner=owner)
            entry["timestamp"] = int(item.ts) if item.ts else now_s
            entry["import_batch"] = batch_id
            entry["origin"] = {"app": "hermes", "profile": item.profile, "file": item.file}
            entry.pop("pinned", None)
            all_mem.append(entry)
            mine.add(_norm(item.text))
            added.append(entry)
        if added:
            memory_manager.save(all_mem)
            for e in added:
                _memory_vector_add(memory_vector, e["id"], e["text"])

    # Chats: one transaction with the original dates.
    from wings.accounts import _db
    from core.database import ChatMessage as DbMsg, Session as DbSession
    Batch = _batch_model()
    Chat = _chat_model()
    done = _imported_chat_keys(owner)
    session_ids: List[str] = []
    db = _db()
    try:
        for item in pend.chats:
            if item.key not in want_c or item.key in done:
                continue
            sid = str(uuid.uuid4())
            started = datetime.utcfromtimestamp(item.started)
            ended = datetime.utcfromtimestamp(item.ended)
            db.add(DbSession(
                id=sid, name=item.title, endpoint_url=endpoint_url or "", model=item.model, owner=owner,
                archived=False, memory_extraction_enabled=False, memory_injection_enabled=False,
                skill_injection_enabled=False, folder=FOLDER, mode="chat", created_at=started,
                updated_at=ended, last_accessed=ended, last_message_at=ended,
                message_count=len(item.messages),
            ))
            db.flush()
            for m in item.messages:
                ts = m["ts"] or item.started
                db.add(DbMsg(
                    id=str(uuid.uuid4()), session_id=sid, role=m["role"], content=m["content"],
                    meta_data=json.dumps({"imported_from": "hermes", "hermes_session_id": item.hermes_id,
                                          "hermes_profile": item.profile, "timestamp": _iso(ts)}),
                    timestamp=datetime.utcfromtimestamp(ts),
                ))
            db.add(Chat(session_id=sid, owner=owner, batch_id=batch_id, chat_key=item.key))
            done.add(item.key)
            session_ids.append(sid)
        counts = {"memories": len(added), "chats": len(session_ids),
                  "messages": sum(len(c.messages) for c in pend.chats if c.key in want_c)}
        if not added and not session_ids:
            db.rollback()
            return {"batch_id": None, "memories": 0, "chats": 0, "session_ids": [], "folder": FOLDER}
        db.add(Batch(id=batch_id, owner=owner, memory_ids=json.dumps([e["id"] for e in added]),
                     counts=json.dumps(counts)))
        db.commit()
    except Exception:
        db.rollback()
        # Keep the memory part undoable even if the chats failed.
        if added:
            _remove_memories(owner, {e["id"] for e in added}, memory_manager, memory_vector)
        with _pending_lock:
            _pending.setdefault(token, pend)   # nothing was kept: the person may retry
        raise
    finally:
        db.close()
    _invalidate_read_only()
    if added:
        _fire("memory_added", owner)
    return {"batch_id": batch_id, "memories": len(added), "chats": len(session_ids),
            "session_ids": session_ids, "folder": FOLDER}


def _fire(event: str, owner: str) -> None:
    try:
        from src.event_bus import fire_event
        fire_event(event, owner)
    except Exception:
        logger.debug("event %s not sent", event, exc_info=True)


def _remove_memories(owner: str, ids: set, memory_manager, memory_vector) -> int:
    if not ids:
        return 0
    all_mem = memory_manager.load_all_for_update()
    keep = [m for m in all_mem if not (m.get("id") in ids and m.get("owner") == owner)]
    removed = len(all_mem) - len(keep)
    if removed:
        memory_manager.save(keep)
        for mid in ids:
            _memory_vector_remove(memory_vector, mid)
    return removed


def batches(owner: str) -> List[Dict[str, Any]]:
    from wings.accounts import _db
    Batch = _batch_model()
    db = _db()
    try:
        rows = db.query(Batch).filter(Batch.owner == owner).order_by(Batch.created_at.desc()).all()
        out = []
        for r in rows:
            try:
                counts = json.loads(r.counts or "{}")
            except ValueError:
                counts = {}
            out.append({"batch_id": r.id, "created_at": r.created_at.strftime("%Y-%m-%dT%H:%M:%SZ") if r.created_at else None,
                        **counts})
        return out
    finally:
        db.close()


def undo(owner: str, batch_id: str, *, memory_manager, memory_vector=None) -> Dict[str, Any]:
    """Remove exactly what one import added for this person.

    Memories go first: if the memory store cannot be written, the batch row
    stays, so Undo can be tried again instead of orphaning those memories.
    """
    from wings.accounts import _db
    from core.database import ChatMessage as DbMsg, Session as DbSession
    Batch = _batch_model()
    Chat = _chat_model()
    db = _db()
    try:
        batch = db.query(Batch).filter(Batch.id == str(batch_id), Batch.owner == owner).first()
        if batch is None:
            raise ImportProblem("That import was not found. It may already be undone.")
        try:
            memory_ids = set(json.loads(batch.memory_ids or "[]"))
        except ValueError:
            memory_ids = set()
        removed = _remove_memories(owner, memory_ids, memory_manager, memory_vector)
        chats = db.query(Chat).filter(Chat.batch_id == batch.id, Chat.owner == owner).all()
        sids = [c.session_id for c in chats]
        if sids:
            db.query(DbMsg).filter(DbMsg.session_id.in_(sids)).delete(synchronize_session=False)
            db.query(DbSession).filter(DbSession.id.in_(sids), DbSession.owner == owner).delete(synchronize_session=False)
            db.query(Chat).filter(Chat.batch_id == batch.id).delete(synchronize_session=False)
        db.delete(batch)
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
    _invalidate_read_only()
    return {"batch_id": batch_id, "memories": removed, "chats": len(sids), "session_ids": sids}
