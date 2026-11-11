"""OpenWings browser backend for upstream Odysseus's ``private_browser`` tool.

Upstream ships the tool schema and the agent-side plumbing but disables every
page action in this build. OpenWings drives a real Chromium in the ``browser``
container (compose profile ``browser``) over the DevTools protocol with
Playwright, the way OpenManus and browser-use do:

  * every action returns the page as a numbered outline of headings, text and
    interactive elements ([e12] button "Search"), so any model, local or
    hosted, text-only or multimodal, can act on it by number,
  * ``screenshot`` additionally returns pixels for multimodal models,
  * each chat drives its own tab, brought to the front so you can watch it in
    the live view (/wings/browser) and take over at any time,
  * logins persist in the browser profile volume; the agent never types
    passwords, one-time codes or card numbers, and hands pay/order/send
    style clicks to you (see wings/browser_policy.py).
"""
from __future__ import annotations

import asyncio
import base64
import collections
import html.parser
import json
import logging
import math
import os
import re
import secrets
import socket
import stat
import time
from pathlib import Path

from wings import browser_policy as policy

logger = logging.getLogger("wings.browser")

STATE_BEGIN = "=== PAGE STATE (newest replaces older ones) ==="
STATE_END = "=== END PAGE STATE ==="
LIVE_VIEW_PATH = "/wings/browser"
# Leading // comment lines are dropped so Playwright sees a plain function.
_DOM_JS = "\n".join(line for line in Path(__file__).with_name("browser_dom.js").read_text(encoding="utf-8").splitlines()
                    if not line.startswith("//")).strip()
_TEXT_JS = "\n".join(line for line in Path(__file__).with_name("browser_text.js").read_text(encoding="utf-8").splitlines()
                     if not line.startswith("//")).strip()
_READ_CHUNK = 7000
_ACTION_TIMEOUT_S = 75
# batch: several page actions in one call (OpenManus's browser code mode does
# this with model written Python; OpenWings runs a bounded list of its own actions,
# each through the same guards as when called alone).
_BATCH_STEP_ACTIONS = frozenset({"open", "navigate", "read", "find", "click", "fill", "press",
                                 "scroll", "wait", "back", "forward", "drag", "hover"})
_BATCH_STEP_ARGS = frozenset({"action", "url", "selector", "target", "ref", "key", "direction", "amount",
                              "timeout_ms", "timeout_s", "text", "value", "find", "x", "y", "to_ref", "to_x", "to_y"})
_BATCH_MAX_STEPS = 20
_BATCH_MAX_ITEMS = 50
_BATCH_MAX_PAGES = 25
_BATCH_MAX_RUNS = 100
_BATCH_TIME_S = 300
_BATCH_OUTPUT_CHARS = 24000
_OP_TIMEOUT_MS = 8000
_NAV_TIMEOUT_MS = 30000
_MAX_TABS = int(os.environ.get("WINGS_BROWSER_MAX_TABS", "10") or 10)
# Where Chromium saves downloads inside the browser container, and where the
# same volume is mounted (read-only) in this container.
_BROWSER_DOWNLOAD_DIR = "/home/seluser/Downloads"
_TEXT_SUFFIXES = (".txt", ".csv", ".tsv", ".json", ".md", ".xml", ".html", ".htm", ".ics", ".vcf", ".log")


def downloads_dir() -> Path:
    return Path(os.environ.get("WINGS_BROWSER_DOWNLOADS", "/workspaces/default/downloads"))


def safe_download_name(name: str) -> str | None:
    """A plain file name inside the downloads folder, or None."""
    name = str(name or "").strip()
    if not name or name in (".", "..") or "/" in name or "\\" in name or "\0" in name or name.startswith("."):
        return None
    return name


_MAX_DOWNLOAD_READ = 25 * 1024 * 1024


def read_download_bytes(name: str | None, *, limit: int = 200 * 1024 * 1024) -> bytes:
    """Bytes of a downloaded file. Refuses anything that is not a regular file
    directly in the downloads folder: symlinks are not followed (the folder is
    writable from the browser container, which must not be able to point the
    app at its own files)."""
    safe = safe_download_name(name or "")
    if not safe:
        raise BrowserError("Give the file name exactly as listed, without folders.")
    folder = downloads_dir()
    try:
        dir_fd = os.open(folder, os.O_RDONLY | os.O_DIRECTORY)
    except OSError:
        raise BrowserError("The downloads folder is not available.") from None
    try:
        try:
            fd = os.open(safe, os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0), dir_fd=dir_fd)
        except OSError:
            raise BrowserError(f"No downloaded file named {_q(safe)}. Downloads so far: "
                               + (", ".join(_q(p.name) for p in list_downloads()[:10]) or "none")) from None
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                raise BrowserError(f"{_q(safe)} is not a regular file.")
            if info.st_size > limit:
                raise BrowserError(f"{safe} is {info.st_size // 1024 // 1024} MB, too large to read here.")
            chunks, total = [], 0
            while True:
                chunk = os.read(fd, 1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > limit:
                    raise BrowserError(f"{safe} is too large to read here.")
                chunks.append(chunk)
            return b"".join(chunks)
        finally:
            os.close(fd)
    finally:
        os.close(dir_fd)


class _TextOnly(html.parser.HTMLParser):
    """Visible text of an HTML document, in linear time."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self._skip = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "noscript", "template"):
            self._skip += 1
        elif tag in ("p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "section", "article"):
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript", "template") and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


def _download_text(name: str, data: bytes) -> str:
    lower = name.lower()
    if lower.endswith(".pdf"):
        import io
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(data))
        return "\n\n".join((page.extract_text() or "") for page in reader.pages[:200])
    if lower.endswith(_TEXT_SUFFIXES):
        text = data.decode("utf-8", "replace")
        if lower.endswith((".html", ".htm")):
            parser = _TextOnly()
            parser.feed(text)
            parser.close()
            text = "".join(parser.parts)
        return text
    raise BrowserError(f"{name} is not a text or PDF file; the person can open it from the live browser's Downloads list.")


async def read_download(name: str, *, part: int = 1) -> str:
    """Text of a downloaded file (text formats and PDF), in parts. The file
    work runs in a thread so a large or hostile file cannot stall the app."""
    safe = safe_download_name(name)
    if not safe:
        raise BrowserError("Give the file name exactly as listed, without folders.")

    def work():
        data = read_download_bytes(safe, limit=_MAX_DOWNLOAD_READ)
        try:
            text = _download_text(safe, data)
        except BrowserError:
            raise
        except Exception as exc:
            raise BrowserError(f"Could not read {safe}: {_short_error(exc)}") from None
        return len(data), text

    size, text = await asyncio.wait_for(asyncio.to_thread(work), 60)
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    parts = max(1, math.ceil(len(text) / _READ_CHUNK))
    part = min(max(int(part or 1), 1), parts)
    chunk = text[(part - 1) * _READ_CHUNK: part * _READ_CHUNK]
    more = f"; read again with amount={part + 1} for more" if part < parts else ""
    return f"Downloaded file {_q(safe)}, {size} bytes (part {part} of {parts}{more}):\n\n{chunk or '(no text found)'}"


_MAX_UPLOAD = 50 * 1024 * 1024


def workspace_file(path: str) -> tuple[str, bytes]:
    """(path relative to /workspaces, bytes) of a file the agent may upload.
    Relative paths are in the default workspace; nothing outside /workspaces,
    no symlinks, at most 50 MB."""
    from wings import files_routes
    raw = str(path or "").strip()
    default = os.environ.get("ODYSSEUS_WORKSPACE_DEFAULT", "/workspaces/default").rstrip("/")
    root = files_routes.ROOT.rstrip("/")
    if raw.startswith("/workspace/"):
        raw = default + raw[len("/workspace"):]
    full = raw if raw.startswith("/") else f"{default}/{raw}"
    if not full.startswith(root + "/"):
        raise BrowserError("Only files in the workspace (/workspaces) can be uploaded.")
    rel = full[len(root) + 1:]
    data = files_routes.read_file_bytes(rel)
    if data is None:
        raise BrowserError(f"No readable file {_q(raw)} in the workspace (symlinks are not followed).")
    if len(data) > _MAX_UPLOAD:
        raise BrowserError(f"{raw} is larger than 50 MB.")
    return rel, data


def _saved_name(suggested: str) -> str:
    """Chromium saves "a.pdf" as "a (1).pdf" when "a.pdf" exists; find the newest match."""
    stem, dot, ext = suggested.rpartition(".")
    if not dot:
        stem, ext = suggested, ""
    pattern = re.compile(re.escape(stem) + r"(?: \(\d+\))?" + (re.escape("." + ext) if ext else "") + "$")
    for path in list_downloads():
        if pattern.match(path.name):
            return path.name
    return suggested


def list_downloads() -> list[Path]:
    """Regular files (not symlinks) directly in the downloads folder, newest first."""
    folder = downloads_dir()
    items = []
    try:
        for entry in os.scandir(folder):
            if (entry.is_file(follow_symlinks=False) and safe_download_name(entry.name)
                    and not entry.name.endswith((".crdownload", ".tmp"))):
                items.append((entry.stat(follow_symlinks=False).st_mtime, Path(entry.path)))
    except OSError:
        return []
    return [p for _, p in sorted(items, key=lambda x: x[0], reverse=True)]


# Accessible name of an element, the same way the page outline computes it
# (aria-label, aria-labelledby, <label>, button value, img alt, svg title,
# text, title, placeholder), so guards see what the agent was shown.
_LABEL_FN = """
  const clean = (s) => String(s || '').replace(/\\s+/g, ' ').trim().slice(0, 120);
  const controlFree = (node) => {
    const parts = []; const w = document.createTreeWalker(node, NodeFilter.SHOW_TEXT); let n;
    while ((n = w.nextNode())) {
      let p = n.parentElement, skip = false;
      while (p && p !== node) { if (['SELECT','OPTION','TEXTAREA','BUTTON'].includes(p.tagName)) { skip = true; break; } p = p.parentElement; }
      if (!skip) parts.push(n.nodeValue);
    }
    return parts.join(' ');
  };
  const labelOf = (el) => {
    if (!el || !el.getAttribute) return '';
    let s = el.getAttribute('aria-label') || '';
    if (!s && el.getAttribute('aria-labelledby')) {
      s = el.getAttribute('aria-labelledby').split(/\\s+/).map(id => {
        const n = (el.getRootNode && el.getRootNode().getElementById) ? el.getRootNode().getElementById(id) : document.getElementById(id);
        return n ? (n.innerText || n.textContent || '') : '';
      }).join(' ');
    }
    if (!s && el.labels && el.labels.length) s = Array.from(el.labels).map(controlFree).join(' ');
    const type = (el.type || '').toLowerCase();
    if (!s && el.tagName === 'INPUT' && ['button', 'submit', 'reset', 'image'].includes(type)) s = el.value || el.alt || '';
    if (!s && el.tagName === 'IMG') s = el.alt || '';
    if (!s && !['SELECT', 'TEXTAREA', 'INPUT'].includes(el.tagName)) s = el.innerText || el.textContent || '';
    if (!s) { const img = el.querySelector && el.querySelector('img[alt]'); if (img) s = img.alt; }
    if (!s) { const t = el.querySelector && el.querySelector('svg title'); if (t) s = t.textContent; }
    if (!s) s = el.getAttribute('title') || el.getAttribute('placeholder') || '';
    return clean(s);
  };
  const ACTIONABLE = 'button, a[href], [role=button], [role=link], [role=menuitem], input[type=submit], input[type=button], input[type=image], summary';
  const describe = (el) => {
    const form = el.form || (el.closest && el.closest('form'));
    const submits = form ? Array.from(form.querySelectorAll('button, input[type=submit], input[type=image], [role=button]'))
        .filter(b => b.tagName !== 'BUTTON' || (b.getAttribute('type') || 'submit').toLowerCase() === 'submit')
        .map(labelOf) : [];
    const action = el.closest ? el.closest(ACTIONABLE) : null;
    const a = el.closest && el.closest('a[href]');
    return {
      tag: el.tagName.toLowerCase(), type: (el.type || '').toLowerCase(),
      autocomplete: el.getAttribute('autocomplete') || '', name: el.getAttribute('name') || '',
      id: el.id || '', placeholder: el.getAttribute('placeholder') || '', label: labelOf(el),
      text: clean(el.innerText || el.textContent),
      value: el.tagName === 'INPUT' && ['button','submit','reset'].includes((el.type||'').toLowerCase()) ? clean(el.value) : '',
      title: clean(el.getAttribute('title')), editable: !!el.isContentEditable,
      actionLabel: action && action !== el ? labelOf(action) : '',
      formSubmits: submits, href: a ? a.href : '',
    };
  };
"""

_INFO_JS = "(el) => {" + _LABEL_FN + "  return describe(el);\n}"

# The deepest focused element of this frame, or null when focus is elsewhere
# (another frame) or nowhere. "inFrame" means focus sits on an <iframe>.
_FOCUSED_JS = "(attr) => {" + _LABEL_FN + """
  const focus = document.hasFocus();
  let el = document.activeElement;
  while (el && el.shadowRoot && el.shadowRoot.activeElement) el = el.shadowRoot.activeElement;
  if (!el || el === document.body || el === document.documentElement) return { none: true, focus };
  if (el.tagName === 'IFRAME' || el.tagName === 'FRAME') return { inFrame: true, focus };
  const info = describe(el);
  info.ref = el.getAttribute(attr) || '';
  info.focus = focus;
  return info;
}"""


def enabled() -> bool:
    flag = os.environ.get("WINGS_BROWSER", "").strip().lower()
    if flag in ("0", "false", "no", "off"):
        return False
    if flag in ("1", "true", "yes", "on"):
        return True
    profiles = os.environ.get("WINGS_COMPOSE_PROFILES", "")
    return "browser" in {p.strip().lower() for p in profiles.split(",")}


def interactive_actions() -> frozenset:
    """Actions the request authority grants on top of upstream's read set
    when the OpenWings browser is on. Guardrails live in the backend itself."""
    actions = {"click", "fill", "type", "press", "select", "new_page", "select_page", "close_page",
               "reload", "console", "network", "drag", "batch", "hover"}
    if policy.allow_eval():
        actions.add("evaluate")
    return frozenset(actions)


_WEB_TASK_RE = re.compile(
    r"\b(?:log|sign)\s*(?:in(?:to)?|on)(?:\s+(?:to|on))?\b.{0,80}\b(?:site|website|portal|account|dashboard|web\s*app|"
    r"[a-z0-9-]+\.(?:com|org|net|gov|edu|io|co|app|ai|au|uk|de|in|ca|nz)\b)"
    r"|\b(?:on|at|via|through|using|from)\s+(?:the|their|our|my|this|that)?\s*(?:\w+\s+){0,2}(?:website|web\s*site|portal|web\s*page|web\s*app|online\s+form)\b"
    r"|\b(?:book|reserve|order|buy|purchase|apply\s+(?:for|to)|register\s+(?:for|on|at)|sign\s+up|enrol+|renew|pay\s+(?:for|my|the)|check\s*out)\b"
    r".{0,120}\b(?:online|website|site|portal|[a-z0-9-]+\.(?:com|org|net|gov|edu|io|co|app|au|uk|de|in|ca|nz))\b"
    r"|\b(?:fill\s+in|complete)\s+(?:the|this|that|an?|my|our)\s+(?:[\w-]+\s+){0,3}form\b"
    r"|\badd\s+.{1,60}\s+to\s+(?:my\s+|the\s+)?(?:cart|basket|bag|wishlist)\b"
    r"|\b(?:download|export)\s+.{1,80}\bfrom\s+(?:the\s+|my\s+|our\s+)?(?:[\w-]+\s+){0,3}(?:site|website|portal|dashboard|account)\b"
    r"|\b(?:use|open|check)\s+(?:my\s+|the\s+|our\s+)?(?:[\w-]+\s+){0,2}(?:portal|dashboard|web\s*app|website)\b"
    r"|\blive\s+browser\b|\btake\s+over\b.{0,40}\bbrowser\b"
    # Signing in to a named service ("Log into Xero", "sign in to LinkedIn").
    r"|\b(?:log|sign)\s*(?:in(?:to)?|on)(?:\s+(?:to|on))?\s+(?:my\s+|our\s+|the\s+)?(?-i:[A-Z][A-Za-z0-9&.-]{1,40})\b",
    re.I | re.S,
)


def is_web_task(text: str) -> bool:
    """Wording that asks for interactive use of a website (used for routing)."""
    return bool(_WEB_TASK_RE.search(str(text or "")))


_BROWSER_WORD_RE = re.compile(r"\b(?:brows(?:er|e|ing)|private[_ -]?browser|live\s+view)\b", re.I)


def is_browser_request(text: str) -> bool:
    """Names the browser, or reads as a web task."""
    value = str(text or "")
    return bool(_BROWSER_WORD_RE.search(value)) or is_web_task(value)


_INTERACTION_RE = re.compile(
    r"\b(?:click|tap|press|fill(?:\s+(?:in|out))?|type|enter|select|choose|tick|check\s+the\s+box|submit|"
    r"scroll|go\s+to|visit|navigate|open\s+(?:the\s+)?(?:link|page|site|tab)|continue|next\s+page|log\s*in|sign\s*in)\b",
    re.I,
)


def browser_task_requested(request_text: str, recent_user_texts=()) -> bool:
    """Whether the person asked for interactive browser work, in this message
    or in one of their last three messages (so "now click Next" continues a
    browser task). Used for the request authority grant."""
    if is_browser_request(request_text):
        return True
    recent = [t for t in recent_user_texts if t and t != request_text][-3:]
    if any(is_browser_request(t) for t in recent):
        return bool(_INTERACTION_RE.search(str(request_text or ""))) or len(str(request_text or "")) < 200
    return False


def _endpoint() -> tuple[str, int]:
    raw = os.environ.get("WINGS_BROWSER_CDP", "browser:9223").strip()
    host, _, port = raw.rpartition(":")
    if not host:
        host, port = raw, "9223"
    return host, int(port or 9223)


class BrowserError(Exception):
    """A failure whose message is safe to show to the model."""


class Handoff(Exception):
    """The person has to do this step in the live view."""


# ---------------------------------------------------------------------------
# Formatting (pure, unit tested)
# ---------------------------------------------------------------------------

def _q(value: str) -> str:
    return json.dumps(str(value), ensure_ascii=False)


def _no_markers(value):
    """Page text may not imitate the page-state markers (they drive retirement)."""
    return value.replace("===", "= = =") if isinstance(value, str) else value


def format_item(item: dict) -> str:
    item = {k: _no_markers(v) for k, v in item.items()}
    if isinstance(item.get("options"), list):
        item["options"] = [_no_markers(o) for o in item["options"]]
    kind = item.get("kind")
    if kind == "heading":
        return "#" * max(1, min(int(item.get("level") or 1), 4)) + " " + item.get("text", "")
    if kind == "text":
        return "  " + item.get("text", "")
    role = item.get("role") or "element"
    parts = [f"[{item.get('ref')}] {role}"]
    name = item.get("name") or ""
    if name:
        parts.append(_q(name))
    sensitive = policy.sensitive_field({
        "type": item.get("type"), "autocomplete": item.get("autocomplete"), "name": item.get("fieldName"),
        "id": item.get("id"), "label": name, "placeholder": item.get("placeholder"),
    }) if role in ("textbox", "searchbox") else None
    typ = item.get("type")
    if typ and typ not in ("text", "textarea", "submit", "button", "checkbox", "radio", "search", "file"):
        parts.append(f"type={typ}")
    if role in ("select", "listbox"):
        if item.get("value"):
            parts.append(f"selected={_q(item['value'])}")
        options = item.get("options") or []
        if options:
            more = int(item.get("optionCount") or len(options)) - len(options)
            parts.append("options: " + " | ".join(options) + (f" (+{more} more)" if more > 0 else ""))
    elif "value" in item and item.get("value") and not sensitive:
        parts.append(f"value={_q(item['value'])}")
    elif item.get("placeholder") and item.get("placeholder") != name:
        parts.append(f"placeholder={_q(item['placeholder'])}")
    if item.get("checked") is not None:
        parts.append("checked" if item["checked"] else "unchecked")
    if item.get("expanded") is not None:
        parts.append("expanded" if item["expanded"] else "collapsed")
    if item.get("selected"):
        parts.append("selected")
    if item.get("required"):
        parts.append("required")
    if item.get("readonly"):
        parts.append("read-only")
    if item.get("disabled"):
        parts.append("disabled")
    if item.get("href"):
        parts.append("→ " + item["href"])
    if item.get("newTab"):
        parts.append("(opens new tab)")
    if item.get("box"):
        x, y, w, h = item["box"]
        parts.append(f"at x={x}..{x + w} y={y}..{y + h} (click or drag inside it with x,y)")
    if item.get("covered"):
        parts.append("(covered by another element)")
    if item.get("hiddenInput"):
        parts.append("(styled; click its label if clicking fails)")
    if sensitive:
        parts.append(f"[{sensitive} field: the person types this in the live view]")
    if role == "file":
        parts.append("[file upload: fill with path= of a workspace file; the person approves it]")
    return " ".join(parts)


def _scroll_line(data: dict) -> str:
    vh = max(int(data.get("viewportHeight") or 1), 1)
    total = max(int(data.get("scrollHeight") or vh), vh)
    y = int(data.get("scrollY") or 0)
    screens = total / vh
    if screens <= 1.05:
        return "Scroll: the whole page fits on screen."
    if y <= 4:
        where = "top of page"
    elif y + vh >= total - 4:
        where = "bottom of page"
    else:
        where = f"{round(100 * y / (total - vh))}% down"
    return f"Scroll: {where}; page is {screens:.1f} screens tall."


def format_state(*, tab_id: int, frames: list[tuple[str, dict]], tabs_line: str = "",
                 dialog: str | None = None, notes: list[str] | None = None) -> str:
    lines = [STATE_BEGIN]
    main = frames[0][1] if frames else {}
    lines.append(f"Tab: t{tab_id}" + (f"   ({tabs_line})" if tabs_line else ""))
    lines.append(f"URL: {_no_markers(main.get('url', ''))}")
    lines.append(f"Title: {_no_markers(main.get('title', ''))}")
    if main.get("viewportHeight"):
        lines.append(_scroll_line(main))
    if dialog:
        lines.append(dialog)
        lines.append(STATE_END)
        return "\n".join(lines)
    if main.get("dialog"):
        lines.append(f"A dialog is open on the page: {_q(main['dialog'])}. Act inside it or close it first.")
    for note in notes or []:
        lines.append(note)
    lines.append("")
    any_items = False
    for index, (label, data) in enumerate(frames):
        items = data.get("items") or []
        if index > 0:
            if not items:
                continue
            lines.append(f"--- inside frame {label} ---")
        for item in items:
            lines.append(format_item(item))
            any_items = True
        hidden_below = int(data.get("below") or 0)
        hidden_above = int(data.get("above") or 0)
        if hidden_below:
            lines.append(f"… {hidden_below} more interactive elements further down (scroll down, or use find).")
        if hidden_above:
            lines.append(f"… {hidden_above} more interactive elements further up (scroll up).")
        if data.get("truncated"):
            lines.append("… very large page: the outline stops early. Use find or read for the rest.")
    if not any_items:
        lines.append("(no visible text or interactive elements; the page may still be loading: use wait, then snapshot)")
    lines.append("")
    lines.append("Use the [eN] refs with click, fill, press and scroll. Refs from older states are invalid.")
    lines.append(STATE_END)
    return "\n".join(lines)


def strip_state(text: str, replacement: str = "[older page state removed; see the newest state]") -> str:
    """Replace every page-state block in ``text`` (used to retire old states)."""
    pattern = re.compile(re.escape(STATE_BEGIN) + r".*?" + re.escape(STATE_END), re.S)
    return pattern.sub(replacement, text)


def _short_error(error: BaseException) -> str:
    text = str(error) or error.__class__.__name__
    text = text.split("=========================== logs", 1)[0]
    text = text.split("Call log:", 1)[0]
    text = re.sub(r"ws://\S+", "ws://…", text)
    return re.sub(r"\s+", " ", text).strip()[:400]


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------

class _Tab:
    def __init__(self, tab_id: int, page, owner: str | None = None):
        self.id = tab_id
        self.page = page
        # OpenWings account that opened or claimed the tab; None for tabs the
        # person opened in the live view (shared among allowed accounts).
        self.owner = owner
        # Unguessable attribute name for element refs, so a page cannot
        # pre-stamp elements with refs the agent will act on.
        self.attr = "data-w" + secrets.token_hex(6)
        self.refs: dict[str, tuple[object, str]] = {}
        self.ref_seq = 0
        self.console = collections.deque(maxlen=40)
        self.network = collections.deque(maxlen=40)
        self.dialog = None
        self.events = collections.deque(maxlen=10)
        self.last_used = time.monotonic()


class _Driver:
    def __init__(self):
        self.lock = asyncio.Lock()
        self.pw = None
        self.browser = None
        self.context = None
        self.tabs: dict[int, _Tab] = {}
        self.by_page: dict[object, _Tab] = {}
        self.counter = 0
        self.session_tab: dict[str, int] = {}
        self.cdp = None
        self.downloads: dict[str, dict] = {}
        self.action_started = 0.0

    # -- connection --------------------------------------------------------
    def connected(self) -> bool:
        try:
            return self.browser is not None and self.browser.is_connected()
        except Exception:
            return False

    async def ensure(self):
        if self.connected() and self.context is not None:
            return
        await self._reset()
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:  # pragma: no cover - image always has it
            raise BrowserError("The Playwright driver is missing from this image. Rebuild with docker compose build.") from exc
        host, port = _endpoint()
        try:
            infos = await asyncio.get_running_loop().getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
        except OSError:
            raise BrowserError(
                "The browser service is not running. Enable it with ./scripts/setup.sh --with-browser "
                "(or add 'browser' to COMPOSE_PROFILES in .env and run docker compose up -d).") from None
        ip = infos[0][4][0]
        # Chromium refuses DevTools requests whose Host header is a name, so connect by address.
        endpoint = f"http://[{ip}]:{port}" if ":" in ip else f"http://{ip}:{port}"
        if self.pw is None:
            self.pw = await async_playwright().start()
        try:
            from wings import keyholder
            if not keyholder.BROWSER_TOKEN:
                raise BrowserError("WINGS_BROWSER_TOKEN is not set. Run ./scripts/setup.sh, then docker compose up -d.")
            self.browser = await self.pw.chromium.connect_over_cdp(
                endpoint, timeout=15000, headers={"X-Wings-Browser-Token": keyholder.BROWSER_TOKEN})
        except BrowserError:
            raise
        except Exception as exc:
            raise BrowserError("Could not connect to the browser service (" + _short_error(exc)[:160] + "). "
                               "Check `docker compose ps browser`.") from None
        self.browser.on("disconnected", lambda *_: self._on_disconnect())
        self.context = self.browser.contexts[0] if self.browser.contexts else await self.browser.new_context()
        self.context.on("page", self._register)
        for page in list(self.context.pages):
            self._register(page)
        await self._watch_downloads()

    async def _watch_downloads(self):
        """Save downloads under their own names in the shared downloads volume.
        Playwright's own download handling assumes the browser runs on this
        machine, so take it over at the browser level."""
        try:
            self.cdp = await self.browser.new_browser_cdp_session()
            self.cdp.on("Browser.downloadWillBegin", self._download_begins)
            self.cdp.on("Browser.downloadProgress", self._download_progress)
            await self.cdp.send("Browser.setDownloadBehavior", {
                "behavior": "allow", "downloadPath": _BROWSER_DOWNLOAD_DIR, "eventsEnabled": True})
        except Exception as exc:
            logger.warning("browser downloads not tracked: %s", _short_error(exc))

    def _download_begins(self, params):
        guid = str(params.get("guid") or "")
        if not guid:
            return
        if len(self.downloads) > 50:
            for old in sorted(self.downloads, key=lambda g: self.downloads[g]["started"])[:10]:
                self.downloads.pop(old, None)
        self.downloads[guid] = {"name": str(params.get("suggestedFilename") or "download"), "state": "inProgress",
                                "bytes": 0, "started": time.monotonic()}

    def _download_progress(self, params):
        item = self.downloads.get(str(params.get("guid") or ""))
        if item is not None:
            item["state"] = str(params.get("state") or item["state"])
            item["bytes"] = int(params.get("receivedBytes") or item["bytes"])

    async def download_notes(self) -> list[str]:
        """Downloads started during the current action, after letting them finish (up to 20 s)."""
        recent = [d for d in self.downloads.values() if d["started"] >= self.action_started]
        if not recent:
            return []
        for _ in range(80):
            if all(d["state"] != "inProgress" for d in recent):
                break
            await asyncio.sleep(0.25)
        notes = []
        for d in recent:
            if d["state"] == "completed":
                name = _saved_name(d["name"])
                notes.append(f"Downloaded {_q(name)} ({d['bytes']} bytes) into the workspace at downloads/{name}. "
                             f"Read it with action=read path={_q(name)}, or with read_file or python "
                             f"(downloads/{name}); the person can save it from Workspace files.")
            elif d["state"] == "canceled":
                notes.append(f"The download of {_q(d['name'])} was cancelled or blocked by the browser.")
            else:
                notes.append(f"{_q(d['name'])} is still downloading; use wait, then read it.")
        return notes

    def _on_disconnect(self):
        self.browser = None
        self.context = None
        self.cdp = None
        self.tabs.clear()
        self.by_page.clear()
        self.session_tab.clear()

    async def _reset(self):
        browser, self.browser = self.browser, None
        self._on_disconnect()
        if browser is not None:
            try:
                await browser.close()
            except Exception:
                pass

    def _register(self, page):
        if page in self.by_page:
            return self.by_page[page]
        self.counter += 1
        tab = _Tab(self.counter, page)
        self.tabs[tab.id] = tab
        self.by_page[page] = tab

        def on_console(msg):
            try:
                tab.console.append(f"{msg.type}: {msg.text}"[:300])
            except Exception:
                pass

        def on_response(resp):
            try:
                if resp.status >= 400:
                    tab.network.append(f"{resp.status} {resp.request.method} {resp.url[:200]}")
            except Exception:
                pass

        def on_failed(req):
            try:
                tab.network.append(f"FAILED {req.method} {req.url[:200]} ({req.failure or 'error'})")
            except Exception:
                pass

        def on_dialog(dialog):
            tab.dialog = dialog

        def on_download(download):
            try:
                tab.events.append(f"Download started: {download.suggested_filename} (saved in the browser's Downloads folder)")
            except Exception:
                pass

        def on_close(*_):
            self.tabs.pop(tab.id, None)
            self.by_page.pop(page, None)

        page.on("console", on_console)
        page.on("response", on_response)
        page.on("requestfailed", on_failed)
        page.on("dialog", on_dialog)
        page.on("download", on_download)
        page.on("close", on_close)
        return tab

    # -- tabs --------------------------------------------------------------
    def _live_tabs(self, owner: str | None = "*") -> list[_Tab]:
        """Open tabs; with an owner, only that account's tabs and the shared ones."""
        tabs = [t for t in sorted(self.tabs.values(), key=lambda t: t.id) if not t.page.is_closed()]
        if owner == "*":
            return tabs
        return [t for t in tabs if t.owner in (None, owner)]

    def _active(self, key: str) -> _Tab | None:
        tab = self.tabs.get(self.session_tab.get(key, -1))
        if tab is not None and not tab.page.is_closed():
            return tab
        return None

    async def _trim_tabs(self, keep: _Tab | None = None):
        """Close least recently used tabs beyond WINGS_BROWSER_MAX_TABS."""
        tabs = self._live_tabs()
        excess = len(tabs) - max(_MAX_TABS, 2) + 1
        for old in sorted(tabs, key=lambda t: t.last_used)[:max(excess, 0)]:
            if old is keep:
                continue
            try:
                await old.page.close()
            except Exception:
                pass

    async def new_tab(self, key: str) -> _Tab:
        await self._trim_tabs()
        page = await self.context.new_page()
        tab = self._register(page)
        tab.owner = _owner_of(key)
        self.session_tab[key] = tab.id
        return tab

    async def _active_or_new(self, key: str) -> _Tab:
        tab = self._active(key)
        if tab is not None:
            tab.last_used = time.monotonic()
            return tab
        claimed = set(self.session_tab.values())
        owner = _owner_of(key)
        for candidate in self._live_tabs(owner):
            if candidate.id not in claimed and candidate.page.url in ("about:blank", ""):
                candidate.owner = owner
                self.session_tab[key] = candidate.id
                return candidate
        return await self.new_tab(key)

    def _tab_by_alias(self, alias: str, key: str) -> _Tab:
        try:
            tab = self.tabs.get(int(str(alias).lstrip("t")))
        except ValueError:
            tab = None
        if tab is None or tab.page.is_closed() or tab.owner not in (None, _owner_of(key)):
            raise BrowserError(f"There is no tab {alias}. Use action=tabs to list them.")
        return tab

    def _tabs_line(self, key: str) -> str:
        tabs = self._live_tabs(_owner_of(key))
        if len(tabs) <= 1:
            return ""
        mine = self.session_tab.get(key)
        return "open tabs: " + ", ".join(
            f"t{t.id}{' (this chat)' if t.id == mine else ''}" for t in tabs)

    # -- observation -------------------------------------------------------
    async def dialog_pending(self, tab: _Tab) -> bool:
        """True while a dialog blocks the page. The person may have answered it
        in the live view, in which case the page responds again."""
        if tab.dialog is None:
            return False
        try:
            await asyncio.wait_for(tab.page.evaluate("1"), 1.0)
        except Exception:
            return True
        tab.dialog = None
        return False

    def _dialog_note(self, tab: _Tab) -> str | None:
        dialog = tab.dialog
        if dialog is None:
            return None
        try:
            kind, message = dialog.type, dialog.message
        except Exception:
            tab.dialog = None
            return None
        return (f"A browser {kind} dialog is waiting: {_q(message[:300])}. "
                "press key=Enter accepts it (fill text= first for a prompt), key=Escape dismisses it.")

    async def observe(self, tab: _Tab, key: str, *, query: str | None = None, notes=None) -> tuple[str, list]:
        page = tab.page
        await self.require_allowed_url(tab)
        await self.dialog_pending(tab)
        dialog = self._dialog_note(tab)
        if dialog:
            return format_state(tab_id=tab.id, frames=[("main", {"url": page.url, "title": ""})],
                                tabs_line=self._tabs_line(key), dialog=dialog, notes=notes), []
        frames_out = []
        seq = tab.ref_seq
        frames = [page.main_frame] + [f for f in page.frames if f is not page.main_frame][:8]
        for index, frame in enumerate(frames):
            if frame.is_detached():
                continue
            if index > 0:
                if frame.url in ("", "about:blank", "about:srcdoc") and not frame.name:
                    continue
                try:
                    handle = await frame.frame_element()
                    box = await handle.bounding_box()
                except Exception:
                    continue
                if not box or box["width"] < 40 or box["height"] < 30:
                    continue
                # Frames on private addresses (or non-web schemes) are never read.
                ok, _, _ = policy.check_url(frame.url)
                if not ok:
                    continue
            args = {"start": seq, "maxItems": 150 if index == 0 else 40, "textBudget": 3500 if index == 0 else 800,
                    "fullPage": bool(query), "query": query or "", "attr": tab.attr}
            try:
                data = await asyncio.wait_for(frame.evaluate(_DOM_JS, args), 10)
            except Exception as exc:
                if index == 0:
                    raise BrowserError("Could not read the page: " + _short_error(exc)) from None
                continue
            seq = max(seq, int(data.get("next") or seq))
            kept = []
            for item in data.get("items") or []:
                if item.get("kind") == "el":
                    ref = str(item.get("ref") or "")
                    known = tab.refs.get(ref)
                    # A ref claimed by another frame is never handed to a second
                    # one (a frame cannot take over a ref the agent already uses).
                    if not re.fullmatch(r"e\d{1,6}", ref) or (known is not None and known[0] is not frame):
                        continue
                    tab.refs[ref] = (frame, data.get("doc") or "")
                kept.append(item)
            data["items"] = kept
            label = "main" if index == 0 else (frame.url.split("?", 1)[0][:80] or frame.name)
            frames_out.append((label, data))
        tab.ref_seq = seq
        if query:
            return "", frames_out
        events = list(tab.events)
        tab.events.clear()
        return format_state(tab_id=tab.id, frames=frames_out, tabs_line=self._tabs_line(key),
                            notes=(notes or []) + events), frames_out

    # -- element lookup ----------------------------------------------------
    async def locate(self, tab: _Tab, args: dict):
        ref = str(args.get("ref") or "").strip()
        if ref:
            ref = ref.strip("[]")
            if not re.fullmatch(r"e\d{1,6}", ref):
                raise BrowserError(f"'{ref}' is not an element ref. Refs look like e12 and come from the latest page state.")
            entry = tab.refs.get(ref)
            if entry is None:
                raise BrowserError(f"{ref} is not on the current page. Use the refs from the newest page state.")
            frame, doc = entry
            try:
                if frame.is_detached() or await frame.evaluate("() => window.__wingsDoc || ''") != doc:
                    raise BrowserError(f"{ref} belongs to an older version of the page (it changed or navigated).")
            except BrowserError:
                raise
            except Exception:
                raise BrowserError(f"{ref} belongs to an older version of the page.") from None
            matches = frame.locator(f'[{tab.attr}="{ref}"]')
            count = await matches.count()
            if count == 0:
                raise BrowserError(f"{ref} is no longer on the page.")
            if count > 1:
                raise BrowserError(f"{ref} is ambiguous on this page; take a new snapshot.")
            return matches.first, ref
        selector = str(args.get("selector") or args.get("target") or "").strip()
        if selector:
            locator = tab.page.locator(selector).first
            if await locator.count() == 0:
                raise BrowserError(f"Nothing on the page matches selector {_q(selector)}.")
            return locator, selector
        text = str(args.get("find") or "").strip()
        if text:
            for candidate in (tab.page.get_by_role("button", name=text), tab.page.get_by_role("link", name=text),
                              tab.page.get_by_label(text), tab.page.get_by_placeholder(text), tab.page.get_by_text(text)):
                locator = candidate.first
                try:
                    if await locator.count() and await locator.is_visible():
                        return locator, _q(text)
                except Exception:
                    continue
            raise BrowserError(f"No visible element matches {_q(text)}.")
        raise BrowserError("Give ref (from the page state), or find=<visible text>, or selector.")

    # -- settling and URL guard ---------------------------------------------
    async def settle(self, page, *, navigation_expected: bool = False):
        await asyncio.sleep(0.35)
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=6000 if navigation_expected else 3000)
        except Exception:
            pass
        try:
            await page.wait_for_load_state("networkidle", timeout=1500)
        except Exception:
            pass

    async def require_allowed_url(self, tab: _Tab):
        blocked = await self.guard_current_url(tab)
        if blocked:
            raise BrowserError(blocked)

    async def focused(self, tab: _Tab):
        """(frame, info) of the element that has keyboard focus, following focus
        into iframes. info is {"none": True} when nothing is focused, and
        (None, None) is returned when it cannot be determined."""
        main = tab.page.main_frame
        try:
            top = await asyncio.wait_for(main.evaluate(_FOCUSED_JS, tab.attr), 3)
        except Exception:
            return None, None
        if not top:
            return None, None
        if not top.get("inFrame"):
            return main, top
        candidates = []
        for frame in [f for f in tab.page.frames if f is not main][:12]:
            if frame.is_detached():
                continue
            try:
                info = await asyncio.wait_for(frame.evaluate(_FOCUSED_JS, tab.attr), 3)
            except Exception:
                return None, None  # a frame we cannot inspect may hold the focus
            if info and not info.get("none") and not info.get("inFrame"):
                candidates.append((frame, info))
        focused = [c for c in candidates if c[1].get("focus")]
        if len(focused) == 1:
            return focused[0]
        if len(candidates) == 1:
            return candidates[0]
        return None, None

    async def guard_current_url(self, tab: _Tab) -> str | None:
        url = tab.page.url
        if url in ("about:blank", "") or url.startswith("chrome-error://"):
            return None
        ok, reason, _ = policy.check_url(url)
        if ok:
            return None
        try:
            await tab.page.goto("about:blank", timeout=5000)
        except Exception:
            pass
        return f"The page went to a blocked address and was closed: {reason}"


_DRIVER = _Driver()


def _owner_of(key: str) -> str:
    return key.split(":", 1)[0]


async def _until_dialog(tab: _Tab, awaitable):
    """Run a page action, but return early (True) if it opens a dialog: the
    action cannot finish until someone answers the dialog."""
    task = asyncio.ensure_future(awaitable)
    while not task.done():
        if tab.dialog is not None:
            task.add_done_callback(lambda t: t.cancelled() or t.exception())
            return True
        await asyncio.sleep(0.1)
    task.result()
    return False


# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------

def _handoff_text(what: str) -> str:
    return (f"PAUSED, needs the person: {what}\n"
            "The agent does not do this step on its own. Stop and tell the user to open the live browser "
            f"(Live browser in the OpenWings sidebar, or {LIVE_VIEW_PATH}), check the page and do this step "
            "themselves, then say when it is done.")


def _named(info: dict) -> str:
    name = info.get("label") or info.get("placeholder") or info.get("name") or ""
    return f" ({_q(name[:60])})" if name else ""


def _value(args: dict) -> str:
    value = args.get("text")
    if value is None:
        value = args.get("value")
    return "" if value is None else str(value)


async def _act(driver: _Driver, args: dict, key: str) -> tuple[str, dict]:
    action = args["action"]
    owner = _owner_of(key)
    if action == "session_info":
        tabs = driver._live_tabs(owner)
        info = {
            "connected": driver.connected(),
            "live_view": LIVE_VIEW_PATH,
            "tabs": [{"tab": f"t{t.id}", "url": t.page.url} for t in tabs],
            "this_chat_tab": (f"t{driver.session_tab[key]}" if key in driver.session_tab else None),
            "guardrails": {
                "private_addresses_allowed": policy.allow_private(),
                "risky_clicks_need_person": policy.confirm_risky(),
                "javascript_allowed": policy.allow_eval(),
                "allowed_domains": policy.allowed_domains() or "any",
            },
        }
        return "session info", {"output": json.dumps(info, indent=1), "exit_code": 0}

    if action == "tabs":
        lines = []
        mine = driver.session_tab.get(key)
        for t in driver._live_tabs(owner):
            try:
                title = await asyncio.wait_for(t.page.title(), 3)
            except Exception:
                title = ""
            lines.append(f"t{t.id}{' (this chat)' if t.id == mine else ''}  {_q(title[:80])}  {t.page.url[:160]}")
        return "tabs", {"output": "\n".join(lines) or "No tabs open.", "exit_code": 0}

    if action == "new_page":
        tab = await driver.new_tab(key)
        page = tab.page
        await page.bring_to_front()
        if args.get("url"):
            return await _navigate(driver, tab, key, args)
        state, _ = await driver.observe(tab, key)
        return "new tab", {"output": f"Opened tab t{tab.id}.\n{state}", "exit_code": 0}

    if action == "select_page":
        if not args.get("page"):
            raise BrowserError("select_page needs page=tN (see action=tabs).")
        tab = driver._tab_by_alias(args["page"], key)
        driver.session_tab[key] = tab.id
        await tab.page.bring_to_front()
        state, _ = await driver.observe(tab, key)
        return f"switched to t{tab.id}", {"output": f"This chat now uses tab t{tab.id}.\n{state}", "exit_code": 0}

    if action in ("close_page", "close"):
        tab = driver._tab_by_alias(args["page"], key) if args.get("page") else driver._active(key)
        if tab is None:
            return "close", {"output": "This chat has no open tab.", "exit_code": 0}
        if len(driver._live_tabs()) <= 1:
            await tab.page.goto("about:blank")
            return "close", {"output": f"Tab t{tab.id} was the last tab, so it was cleared instead of closed.", "exit_code": 0}
        await tab.page.close()
        return "close", {"output": f"Closed tab t{tab.id}.", "exit_code": 0}

    if action == "batch":
        return await _batch(driver, args, key)

    tab = driver._tab_by_alias(args["page"], key) if args.get("page") else await driver._active_or_new(key)
    tab.last_used = time.monotonic()
    page = tab.page

    if action in ("open", "navigate"):
        if not args.get("url"):
            raise BrowserError(f"{action} needs url.")
        return await _navigate(driver, tab, key, args)

    if action in ("snapshot",):
        await page.bring_to_front()
        state, _ = await driver.observe(tab, key)
        return "snapshot", {"output": state, "exit_code": 0}

    if action == "screenshot":
        await page.bring_to_front()
        state, _ = await driver.observe(tab, key)
        if tab.dialog is not None:
            return "screenshot", {"output": "No screenshot while a dialog is open.\n" + state, "exit_code": 0}
        marks = await _draw_marks(page, tab)
        try:
            shot = await page.screenshot(type="jpeg", quality=60, timeout=10000)
        finally:
            if marks is not None:
                try:
                    await marks.evaluate("(host) => host.remove()")
                    await marks.dispose()
                except Exception:
                    pass
        await driver.require_allowed_url(tab)  # discard it if the page moved somewhere blocked meanwhile
        labelled = " Boxes labelled e1, e2, ... are the refs in the page state below." if marks is not None else ""
        return "screenshot", {"output": "Screenshot of the visible part of the page attached." + labelled + "\n" + state,
                              "exit_code": 0,
                              "images": [{"mimeType": "image/jpeg", "data": base64.b64encode(shot).decode()}]}

    if action == "read" and args.get("path"):
        return "read download", {"output": await read_download(str(args["path"]), part=int(args.get("amount") or 1)),
                                 "exit_code": 0}

    if action == "read":
        await driver.require_allowed_url(tab)
        if await driver.dialog_pending(tab):
            state, _ = await driver.observe(tab, key)
            return "read", {"output": state, "exit_code": 0}
        if args.get("ref") or args.get("selector") or args.get("target"):
            locator, label = await driver.locate(tab, args)
            text = await locator.inner_text(timeout=_OP_TIMEOUT_MS)
            source = label
        else:
            try:
                text = await asyncio.wait_for(page.main_frame.evaluate(_TEXT_JS), 20)
            except Exception:
                text = await page.main_frame.evaluate("() => document.body ? document.body.innerText : ''")
            source = page.url
        await driver.require_allowed_url(tab)  # the page may have redirected while it was read
        text = re.sub(r"[ \t]+\n", "\n", text or "")
        text = re.sub(r"\n{3,}", "\n\n", text).strip()
        parts = max(1, math.ceil(len(text) / _READ_CHUNK))
        part = int(args.get("amount") or 1)
        part = min(max(part, 1), parts)
        chunk = text[(part - 1) * _READ_CHUNK: part * _READ_CHUNK]
        head = f"Text of {source} (part {part} of {parts}"
        head += f"; read again with amount={part + 1} for more)" if part < parts else ")"
        title = await page.title()
        return "read", {"output": f"{head}\nTitle: {title}\n\n{chunk or '(no text)'}", "exit_code": 0}

    if action == "find":
        query = str(args.get("find") or args.get("text") or "").strip()
        if not query:
            raise BrowserError("find needs text=<words to look for>.")
        _, frames = await driver.observe(tab, key, query=query)
        lines = []
        for label, data in frames:
            for item in data.get("items") or []:
                lines.append(format_item(item) + ("" if label == "main" else f"   (in frame {label})"))
        if not lines:
            return "find", {"output": f"Nothing on {page.url} matches {_q(query)}.", "exit_code": 0}
        return "find", {"output": f"Matches for {_q(query)} on {page.url} (use the refs directly):\n" + "\n".join(lines[:60]),
                        "exit_code": 0}

    if action in ("back", "forward", "reload"):
        fn = {"back": page.go_back, "forward": page.go_forward, "reload": page.reload}[action]
        await fn(timeout=_NAV_TIMEOUT_MS, wait_until="domcontentloaded")
        await driver.settle(page)
        return await _state_result(driver, tab, key, action, f"Went {action}." if action != "reload" else "Reloaded.")

    if action == "wait":
        timeout_ms = int(args.get("timeout_ms") or 0) or int(float(args.get("timeout_s") or 0) * 1000) or 2000
        timeout_ms = min(max(timeout_ms, 100), 30000)
        text = str(args.get("text") or "").strip()
        if text:
            try:
                await page.get_by_text(text).first.wait_for(state="visible", timeout=timeout_ms)
                note = f"{_q(text)} is now visible."
            except Exception:
                note = f"{_q(text)} did not appear within {timeout_ms} ms."
        else:
            await asyncio.sleep(timeout_ms / 1000)
            note = f"Waited {timeout_ms} ms."
        return await _state_result(driver, tab, key, "wait", note)

    if action == "scroll":
        await page.bring_to_front()
        if args.get("ref") or args.get("selector") or args.get("target") or args.get("find"):
            locator, label = await driver.locate(tab, args)
            await locator.scroll_into_view_if_needed(timeout=_OP_TIMEOUT_MS)
            note = f"Scrolled {label} into view."
        elif args.get("text"):
            locator = page.get_by_text(str(args["text"])).first
            if not await locator.count():
                raise BrowserError(f"No text {_q(args['text'])} on the page.")
            await locator.scroll_into_view_if_needed(timeout=_OP_TIMEOUT_MS)
            note = f"Scrolled to {_q(args['text'])}."
        else:
            direction = str(args.get("direction") or "down").lower()
            size = await page.evaluate("() => [innerWidth, innerHeight]")
            amount = float(args.get("amount") or 1)
            pixels = amount if amount > 10 else amount * size[1] * 0.85
            dx, dy = {"down": (0, pixels), "up": (0, -pixels), "right": (pixels, 0), "left": (-pixels, 0)}.get(direction, (0, pixels))
            await page.mouse.move(size[0] / 2, size[1] / 2)
            await page.mouse.wheel(dx, dy)
            await asyncio.sleep(0.5)
            note = f"Scrolled {direction}."
        return await _state_result(driver, tab, key, "scroll", note)

    if action == "click":
        await page.bring_to_front()
        if await driver.dialog_pending(tab):
            raise BrowserError("A browser dialog is waiting. Use press key=Enter to accept or key=Escape to dismiss it.")
        point = _point(args, "x", "y")
        if point is not None and not any(args.get(k) for k in ("ref", "selector", "target", "find")):
            info = await _hover_then_check(page, *point)
            label = f"the point ({point[0]}, {point[1]})"

            async def perform():
                await page.mouse.down()
                await page.mouse.up()
        else:
            locator, label = await driver.locate(tab, args)
            info = await locator.evaluate(_INFO_JS)

            def perform():
                return locator.click(timeout=_OP_TIMEOUT_MS)
        _check_risky_click(info)
        if info.get("href") and not info["href"].startswith(("javascript:", "#")):
            ok, reason, _ = policy.check_url(info["href"])
            if not ok and not info["href"].startswith(page.url.split("#", 1)[0] + "#"):
                raise BrowserError(f"That link is blocked: {reason}")
        before = set(driver.context.pages)
        try:
            if await _until_dialog(tab, perform()):
                return await _state_result(driver, tab, key, "click", f"Clicked {label}; the page opened a dialog.")
        except Exception as exc:
            message = _short_error(exc)
            covered = re.search(r"(<[^>]{1,160}>)[^<]{0,80}intercepts pointer events", message)
            if covered:
                raise BrowserError(f"Could not click {label}: another element is on top of it ({covered.group(1)}). "
                                   "Close or accept the popup or banner first.") from None
            raise BrowserError(f"Could not click {label}: {message}") from None
        await driver.settle(page, navigation_expected=True)
        note = f"Clicked {label}" + (f" ({_q(info.get('label') or info.get('text'))[:80]})" if (info.get('label') or info.get('text')) else "") + "."
        new_pages = [p for p in driver.context.pages if p not in before]
        if new_pages:
            new_tab = driver._register(new_pages[-1])
            new_tab.owner = owner
            driver.session_tab[key] = new_tab.id
            try:
                await new_tab.page.wait_for_load_state("domcontentloaded", timeout=8000)
            except Exception:
                pass
            await new_tab.page.bring_to_front()
            note += f" It opened a new tab, t{new_tab.id}, which this chat now uses."
            tab = new_tab
        return await _state_result(driver, tab, key, "click", note)

    if action == "hover":
        # Menus and tooltips that open only under the pointer (patch 0018).
        # Hovering commits nothing; clicks on what it reveals are guarded.
        await page.bring_to_front()
        if await driver.dialog_pending(tab):
            raise BrowserError("A browser dialog is waiting. Use press key=Enter or key=Escape first.")
        point = _point(args, "x", "y")
        if point is not None and not any(args.get(k) for k in ("ref", "selector", "target", "find")):
            await _point_info(page, *point)
            await page.mouse.move(*point)
            label = f"the point ({point[0]}, {point[1]})"
        else:
            locator, label = await driver.locate(tab, args)
            await locator.hover(timeout=_OP_TIMEOUT_MS)
        await asyncio.sleep(0.4)  # hover menus often open after a short delay
        return await _state_result(driver, tab, key, "hover",
                                   f"The pointer is over {label}; anything it revealed is in the outline below.")

    if action == "drag":
        await page.bring_to_front()
        if await driver.dialog_pending(tab):
            raise BrowserError("A browser dialog is waiting. Use press key=Enter or key=Escape first.")
        to_point, from_point = _point(args, "to_x", "to_y"), _point(args, "x", "y")
        # A screen point only means something while nothing scrolls, so with
        # a point at either end neither end is scrolled into view. With two
        # elements both are scrolled to, then both boxes are measured again
        # (a later scroll, of the window or of an inner list, moves the other).
        scroll = to_point is None and from_point is None
        end = await _drag_end(driver, tab, args.get("to_ref"), to_point, args.get("text"), scroll=scroll)
        start = await _drag_end(driver, tab, args.get("ref"), from_point, args.get("find"), scroll=scroll)
        if scroll:
            end = await _drag_end(driver, tab, args.get("to_ref"), None, args.get("text"), scroll=False)
            start = await _drag_end(driver, tab, args.get("ref"), None, args.get("find"), scroll=False)
        view = await page.evaluate(_VIEW_JS)
        for x, y in (start[:2], end[:2]):
            if not (0 <= x < view[2] and 0 <= y < view[3]):
                raise BrowserError("Both ends of a drag must be on screen at once. Scroll so they are, take a "
                                   "screenshot, then drag (or drag in shorter steps).")
        for info in (start[2], end[2]):
            _check_risky_click(info)
        _check_drop_target(end[2])
        if start[3] is not None:
            await _hover_locator_then_check(page, start[3])   # works for refs inside frames too
        else:
            await _hover_then_check(page, int(start[0]), int(start[1]))
        await page.mouse.down()
        # A small first move starts an HTML5 drag (Playwright only picks the
        # drag up between separate moves), then glide to the target so pages
        # that track plain mouse events see the path too.
        await page.mouse.move(start[0] + 6, start[1])
        await page.mouse.move(end[0], end[1], steps=12)
        # Boards often show a Delete or Remove zone only while something is
        # dragged: check what is under the release point now, and drop the
        # item back where it came from if that would commit something.
        try:
            await asyncio.sleep(0.1)
            if end[3] is not None:
                at = await end[3].evaluate(_POINT_AT_JS)
                if not at or at.get("outside") or at.get("frame"):
                    at = await end[3].evaluate(_INFO_JS)
                release = _merge_deep(at, await _deep_info(page, end[0], end[1]))
            else:
                release = await _point_info(page, int(end[0]), int(end[1]))
            _check_risky_click(release)
            _check_drop_target(release)
        except (Handoff, BrowserError):
            try:
                await page.keyboard.press("Escape")
                await page.mouse.move(start[0], start[1], steps=6)
            finally:
                await page.mouse.up()
            raise
        await page.mouse.up()
        await driver.settle(page)
        return await _state_result(driver, tab, key, "drag",
                                   f"Dragged from ({start[0]:.0f}, {start[1]:.0f}) to ({end[0]:.0f}, {end[1]:.0f}).")

    if action == "fill":
        await page.bring_to_front()
        value = _value(args)
        if await driver.dialog_pending(tab):
            if tab.dialog.type != "prompt":
                raise BrowserError("A browser dialog is waiting. Use press key=Enter or key=Escape.")
            dialog, tab.dialog = tab.dialog, None
            await dialog.accept(value)
            await driver.settle(page)
            return await _state_result(driver, tab, key, "fill", "Answered the prompt dialog.")
        locator, label = await driver.locate(tab, args)
        info = await locator.evaluate(_INFO_JS)
        sensitive = policy.sensitive_field(info)
        if sensitive:
            raise Handoff(f"{label} is a {sensitive} field. Only the person types those, in the live view.")
        tag, typ = info.get("tag"), info.get("type")
        if typ == "file":
            if not args.get("path"):
                raise BrowserError("This is a file upload field. Give path= of a file in the workspace "
                                   "(for example report.pdf or downloads/invoice.pdf).")
            rel, data = workspace_file(str(args["path"]))
            import mimetypes
            mime = mimetypes.guess_type(rel)[0] or "application/octet-stream"
            await locator.set_input_files({"name": os.path.basename(rel), "mimeType": mime, "buffer": data},
                                          timeout=_OP_TIMEOUT_MS)
            await asyncio.sleep(0.3)
            return await _state_result(driver, tab, key, "fill",
                                       f"Attached {_q(rel)} ({len(data)} bytes) to {label}{_named(info)}.")
        if args.get("path"):
            raise BrowserError(f"{label} is not a file upload field; path= only works on file inputs.")
        if tag == "select":
            try:
                await locator.select_option(label=value, timeout=_OP_TIMEOUT_MS)
            except Exception:
                try:
                    await locator.select_option(value=value, timeout=_OP_TIMEOUT_MS)
                except Exception:
                    raise BrowserError(f"{label} has no option {_q(value)}.") from None
            note = f"Selected {_q(value)} in {label}{_named(info)}."
        elif typ in ("checkbox", "radio"):
            want = value.strip().lower() not in ("false", "no", "off", "0", "unchecked", "uncheck")
            try:
                await locator.set_checked(want, timeout=_OP_TIMEOUT_MS)
            except Exception:
                # Styled controls hide the real input; its label is the click target.
                await locator.evaluate("(el, want) => { if (el.checked !== want) ((el.labels && el.labels[0]) || el).click(); }", want)
            note = f"{'Checked' if want else 'Unchecked'} {label}{_named(info)}."
        elif info.get("editable") and tag not in ("input", "textarea"):
            await locator.click(timeout=_OP_TIMEOUT_MS)
            await page.keyboard.press("ControlOrMeta+A")
            await page.keyboard.type(value, delay=10)
            note = f"Typed into {label}{_named(info)}."
        else:
            await locator.fill(value, timeout=_OP_TIMEOUT_MS)
            note = f"Filled {label}{_named(info)}."
        await asyncio.sleep(0.25)
        return await _state_result(driver, tab, key, "fill", note)

    if action == "press":
        await page.bring_to_front()
        key_name = str(args.get("key") or "").strip()
        text = args.get("text")
        if await driver.dialog_pending(tab):
            dialog, tab.dialog = tab.dialog, None
            normalized = policy.normalize_key(key_name) or ""
            if normalized == "Enter":
                if policy.confirm_risky() and policy.risky_text(dialog.message[:300]):
                    tab.dialog = dialog
                    raise Handoff(f"the dialog {_q(dialog.message[:120])} asks to confirm a committing action.")
                await dialog.accept(str(text) if text is not None and dialog.type == "prompt" else "")
                note = "Accepted the dialog."
            else:
                await dialog.dismiss()
                note = "Dismissed the dialog."
            await driver.settle(page)
            return await _state_result(driver, tab, key, "press", note)
        label = "the focused element"
        if args.get("ref") or args.get("selector") or args.get("target") or args.get("find"):
            locator, label = await driver.locate(tab, args)
            await locator.focus(timeout=_OP_TIMEOUT_MS)
        # Whatever has keyboard focus now, looking inside iframes, is what the
        # keys reach; the guards check that element, not just the one named.
        _, info = await driver.focused(tab)
        if text is not None and not key_name:
            if info is None or info.get("none"):
                raise BrowserError("Cannot tell which field has focus. Give ref= of the field to type into.")
            sensitive = policy.sensitive_field(info)
            if sensitive:
                raise Handoff(f"{label} is a {sensitive} field. Only the person types those, in the live view.")
            await page.keyboard.type(str(text), delay=25)
            await asyncio.sleep(0.3)
            return await _state_result(driver, tab, key, "press", f"Typed {len(str(text))} characters into {label}.")
        normalized = policy.normalize_key(key_name)
        if not normalized:
            raise BrowserError(f"Unknown key {_q(key_name)}. Use names like Enter, Tab, Escape, ArrowDown or Control+A.")
        if policy.is_paste_key(normalized):
            raise BrowserError("Pasting is not available to the agent. Use fill, or press with text=.")
        base = normalized.rsplit("+", 1)[-1] if normalized != "+" else "+"
        if info is not None and not info.get("none"):
            sensitive = policy.sensitive_field(info)
            if sensitive and base not in ("Tab", "Escape", "Enter"):
                raise Handoff(f"{label} is a {sensitive} field. Only the person types those, in the live view.")
        elif info is None and len(base) == 1:
            raise BrowserError("Cannot tell which element has focus. Give ref= of the element.")
        if base in ("Enter", " ") and policy.confirm_risky():
            if info is None:
                raise BrowserError("Cannot tell what this key would activate. Click the button by ref instead.")
            if not info.get("none"):
                risky = policy.risky_label(info.get("label"), info.get("text"), info.get("value"), info.get("title"),
                                           info.get("actionLabel"), *(info.get("formSubmits") or []))
                if risky:
                    raise Handoff(f"pressing {'Enter' if base == 'Enter' else 'Space'} here would activate {_q(risky)}.")
        if await _until_dialog(tab, page.keyboard.press(normalized)):
            return await _state_result(driver, tab, key, "press", f"Pressed {normalized}; the page opened a dialog.")
        await driver.settle(page, navigation_expected=base == "Enter")
        return await _state_result(driver, tab, key, "press", f"Pressed {normalized} on {label}.")

    if action == "console":
        return "console", {"output": "\n".join(tab.console) or "No console messages recorded for this tab.", "exit_code": 0}

    if action == "network":
        return "network", {"output": "\n".join(tab.network) or "No failed or error responses recorded for this tab.",
                           "exit_code": 0}

    if action == "evaluate":
        if not policy.allow_eval():
            raise BrowserError("Running JavaScript in pages is turned off (WINGS_BROWSER_ALLOW_EVAL=0). "
                               "Use snapshot, find, read, click and fill instead.")
        script = str(args.get("script") or "").strip()
        if not script:
            raise BrowserError("evaluate needs script.")
        result = await asyncio.wait_for(page.evaluate(script), 15)
        text = json.dumps(result, default=str, ensure_ascii=False)
        return "evaluate", {"output": text[:4000], "exit_code": 0}

    raise BrowserError(f"Unsupported action {action}.")


# Boxes of the ref'd elements visible in one frame, for screenshot labels.
_MARK_BOXES_JS = """(attr) => {
  const out = [];
  for (const el of document.querySelectorAll('[' + attr + ']')) {
    const ref = el.getAttribute(attr);
    if (!/^e\\d{1,6}$/.test(ref || '')) continue;
    const r = el.getBoundingClientRect();
    if (r.width < 2 || r.height < 2 || r.bottom < 0 || r.right < 0 || r.top > innerHeight || r.left > innerWidth) continue;
    out.push([ref, r.left, r.top, r.width, r.height]);
    if (out.length >= 300) break;
  }
  return out;
}"""

# Draws numbered boxes over the page for one screenshot (removed right after).
# A closed shadow root keeps page styles and scripts out of the labels.
_MARKS_DRAW_JS = """(boxes) => {
  const host = document.createElement('div');
  host.style.cssText = 'position:fixed;left:0;top:0;width:0;height:0;overflow:visible;z-index:2147483647;pointer-events:none;';
  const root = host.attachShadow({ mode: 'closed' });
  const colors = ['#e6194b', '#3cb44b', '#4363d8', '#f58231', '#911eb4', '#008080', '#9a6324', '#800000', '#000075', '#808000'];
  boxes.forEach((b, i) => {
    const [ref, x, y, w, h] = b;
    const c = colors[i % colors.length];
    const box = document.createElement('div');
    box.style.cssText = `position:fixed;left:${x}px;top:${y}px;width:${w}px;height:${h}px;border:2px solid ${c};box-sizing:border-box;`;
    const tag = document.createElement('div');
    tag.textContent = ref;
    // Just right of the box (usually empty space in forms and lists),
    // inside its right edge when the box reaches the edge of the screen.
    const lw = 8 + 7 * ref.length;
    const lx = x + w + 2 + lw <= innerWidth ? x + w + 2 : Math.max(0, x + w - lw);
    tag.style.cssText = `position:fixed;left:${lx}px;top:${Math.max(0, y)}px;background:${c};color:#fff;font:bold 11px/14px monospace;padding:0 3px;border-radius:2px;`;
    root.appendChild(box);
    root.appendChild(tag);
  });
  document.documentElement.appendChild(host);
  return host;
}"""

# What is under a screen point, seen the way a click experiences it: the
# innermost element (inside open shadow roots), plus everything on its
# composed path up to the document: the nearest button or link (across shadow
# boundaries, so an icon inside a labelled "Delete" button is the Delete
# button) and the labels of the shadow hosts on the way.
_COMPOSED_FN = """
  const composed = (el, hosts) => {
    const info = describe(el);
    let action = null, link = null;
    for (let n = el, i = 0; n && i < 256; i++) {
      if (!action && n.matches && n.matches(ACTIONABLE)) action = n;
      if (!link && n.matches && n.matches('a[href]')) link = n;
      const root = n.getRootNode ? n.getRootNode() : null;
      n = n.parentElement || (root && root.host) || null;
      if (n && root && root.host === n && !hosts.includes(n)) hosts.push(n);
    }
    if (action && action !== el) info.actionLabel = info.actionLabel || labelOf(action);
    if (link && !info.href) info.href = link.href;
    const hostLabels = hosts.map(h => clean(h.getAttribute('aria-label') || h.getAttribute('title') || '')).filter(Boolean);
    if (hostLabels.length) info.hostLabel = hostLabels.join(' | ').slice(0, 240);
    return info;
  };
"""
_POINT_BODY = """
  if (x < 0 || y < 0 || x >= view.innerWidth || y >= view.innerHeight) return { outside: true };
  let el = doc.elementFromPoint(x, y);
  if (!el) return { outside: true };
  const hosts = [];
  for (let depth = 0; depth < 32 && el.shadowRoot; depth++) {
    const inner = el.shadowRoot.elementFromPoint(x, y);
    if (!inner || inner === el) break;
    hosts.push(el);
    el = inner;
  }
  if (['IFRAME', 'FRAME', 'OBJECT', 'EMBED'].includes(el.tagName)) return { frame: true };
  const info = composed(el, hosts);
  // A custom element whose insides cannot be read (closed shadow root) and
  // that has no label anywhere on its path could be anything, a pay button
  // included (the browser's own lookup below usually reads it anyway).
  if (el.tagName.includes('-') && !el.shadowRoot
      && !(info.label || info.text || info.title || info.actionLabel || info.hostLabel)) {
    info.opaque = true;
  }
  return info;
}"""
_POINT_JS = ("([x, y]) => { const doc = document, view = window;" + _LABEL_FN + _COMPOSED_FN
             + _POINT_BODY)
# The same, at the centre of an element in its own frame (a ref inside an iframe).
_POINT_AT_JS = ("(start) => { const doc = start.ownerDocument, view = doc.defaultView;"
                " const r = start.getBoundingClientRect(); const x = r.left + r.width / 2, y = r.top + r.height / 2;"
                + _LABEL_FN + _COMPOSED_FN + _POINT_BODY)
# Chromium's own hit test (DOM.getNodeForLocation) also finds nodes inside
# closed shadow roots, which page scripts cannot see; describe that node too.
_DEEP_FN = ("function() { let el = this && this.nodeType === 3 ? this.parentElement : this;"
            " if (!el || !el.tagName) return null;" + _LABEL_FN + _COMPOSED_FN
            + " return composed(el, []); }")


_FRAMES_JS = "() => new Promise(r => requestAnimationFrame(() => requestAnimationFrame(() => r(true))))"
_VIEW_JS = "() => [scrollX, scrollY, innerWidth, innerHeight]"


def _point(args: dict, kx: str, ky: str):
    if args.get(kx) is None or args.get(ky) is None:
        return None
    try:
        x, y = int(args[kx]), int(args[ky])
    except (TypeError, ValueError):
        raise BrowserError(f"{kx} and {ky} must be whole numbers (pixels in the latest screenshot).") from None
    if x < 0 or y < 0 or x > 20000 or y > 20000:
        raise BrowserError(f"{kx}, {ky} are outside the page.")
    return x, y


async def _deep_info(page, x: float, y: float) -> dict | None:
    """Chromium's own description of the node under a point (sees closed
    shadow roots). None when it cannot be asked."""
    session = None
    try:
        session = await page.context.new_cdp_session(page)
        await session.send("DOM.getDocument", {"depth": 0})
        node = await session.send("DOM.getNodeForLocation", {
            "x": int(x), "y": int(y), "includeUserAgentShadowDOM": False, "ignorePointerEventsNone": True})
        resolved = await session.send("DOM.resolveNode", {"backendNodeId": node["backendNodeId"]})
        result = await session.send("Runtime.callFunctionOn", {
            "objectId": resolved["object"]["objectId"], "functionDeclaration": _DEEP_FN, "returnByValue": True})
        value = (result.get("result") or {}).get("value")
        return value if isinstance(value, dict) else None
    except Exception as exc:
        logger.debug("deep point lookup skipped: %s", _short_error(exc))
        return None
    finally:
        if session is not None:
            try:
                await session.detach()
            except Exception:
                pass


def _merge_deep(info: dict, deep: dict | None) -> dict:
    if deep:
        info["deep"] = {k: deep.get(k) for k in ("tag", "type", "autocomplete", "name", "id", "placeholder", "label",
                                                 "text", "value", "title", "actionLabel", "hostLabel", "href", "editable")}
        if deep.get("href") and not info.get("href"):
            info["href"] = deep["href"]
        if any(deep.get(k) for k in ("label", "text", "title", "actionLabel", "hostLabel")):
            info.pop("opaque", None)
    return info


async def _point_info(page, x: int, y: int) -> dict:
    """What is under a screen point, described like a ref'd element, so the
    same guards apply to pointer clicks as to clicks by ref."""
    info = await page.evaluate(_POINT_JS, [x, y])
    if not info or info.get("outside"):
        raise BrowserError(f"({x}, {y}) is outside the visible page. Take a screenshot to see the current layout.")
    if info.get("frame"):
        raise BrowserError("That point is inside an embedded frame or object; act on the refs inside it instead.")
    info = _merge_deep(info, await _deep_info(page, x, y))
    if info.get("opaque") and policy.confirm_risky():
        raise Handoff("that point is on a page component whose label cannot be read, so OpenWings cannot "
                      "tell whether clicking it commits something.")
    return info


async def _hover_locator_then_check(page, locator) -> dict:
    """_hover_then_check for an element (also inside an iframe): hover it,
    then check what is under the pointer in the element's own frame."""
    await locator.hover(timeout=_OP_TIMEOUT_MS)
    try:
        await asyncio.wait_for(locator.evaluate("() => new Promise(r => requestAnimationFrame(() => r(true)))"), 2)
    except Exception:
        pass
    info = {}
    for attempt in range(2):
        if attempt:
            await asyncio.sleep(0.15)
        info = await locator.evaluate(_POINT_AT_JS)
        if not info or info.get("outside") or info.get("frame"):
            info = await locator.evaluate(_INFO_JS)
        box = await locator.bounding_box()
        if box:
            info = _merge_deep(info, await _deep_info(page, box["x"] + box["width"] / 2, box["y"] + box["height"] / 2))
        _check_risky_click(info)
    return info


async def _hover_then_check(page, x: int, y: int) -> dict:
    """Move the pointer to the point first and check what is under it after
    the page reacted: rows and tiles often reveal Delete or Buy now buttons
    on hover, exactly where the pointer then presses."""
    await page.mouse.move(x, y)
    try:
        await asyncio.wait_for(page.evaluate(_FRAMES_JS), 2)
    except Exception:
        pass
    first = await _point_info(page, x, y)
    _check_risky_click(first)
    await asyncio.sleep(0.15)  # hover transitions with a short delay
    info = await _point_info(page, x, y)
    _check_risky_click(info)
    return info


def _check_drop_target(info: dict):
    """A drop must not put text into a field: fill and press guard fields
    (passwords, one time codes and card numbers stay the person's)."""
    if info.get("deep"):
        _check_drop_target({k: v for k, v in info["deep"].items() if v is not None})
    why = policy.sensitive_field(info)
    if why:
        raise Handoff(f"dropping onto the {why} field would put a value there without you; type it yourself.")
    tag, kind = info.get("tag"), str(info.get("type") or "").lower()
    if info.get("editable") or tag == "textarea" or (tag == "input" and kind not in (
            "button", "submit", "reset", "image", "checkbox", "radio", "range", "color", "file")):
        raise BrowserError("Dropping onto a text field is not supported; use fill to type into it.")


async def _draw_marks(page, tab: _Tab):
    """Label the refs on screen (set of marks) so a vision model can match
    what it sees to the refs it acts on. Returns the overlay handle, or None
    when nothing could be labelled; never fails the screenshot."""
    boxes = []
    try:
        frames = {id(frame): frame for frame, _ in tab.refs.values()}.values()
        for frame in frames:
            if frame.is_detached():
                continue
            dx = dy = 0.0
            if frame is not page.main_frame:
                handle = await frame.frame_element()
                fbox = await handle.bounding_box()
                if not fbox:
                    continue
                dx, dy = fbox["x"], fbox["y"]
            for ref, x, y, w, h in await asyncio.wait_for(frame.evaluate(_MARK_BOXES_JS, tab.attr), 5):
                if ref in tab.refs and tab.refs[ref][0] is frame:
                    boxes.append([ref, round(x + dx), round(y + dy), round(w), round(h)])
        if not boxes:
            return None
        return await asyncio.wait_for(page.main_frame.evaluate_handle(_MARKS_DRAW_JS, boxes[:300]), 5)
    except Exception as exc:
        logger.debug("screenshot labels skipped: %s", _short_error(exc))
        return None


async def _drag_end(driver: _Driver, tab: _Tab, ref, point, text=None, *, scroll=True):
    """(x, y, info, locator or None) for one end of a drag: a ref's centre, a
    screen point, or the centre of the element showing some visible text
    (drop zones usually have no ref)."""
    if ref or text:
        locator, _ = await driver.locate(tab, {"ref": ref} if ref else {"find": text})
        if scroll:
            await locator.scroll_into_view_if_needed(timeout=_OP_TIMEOUT_MS)
        box = await locator.bounding_box()
        if not box:
            raise BrowserError(f"{ref or _q(text)} is not visible.")
        info = await locator.evaluate(_INFO_JS)
        return box["x"] + box["width"] / 2, box["y"] + box["height"] / 2, info, locator
    if point is not None:
        return point[0], point[1], await _point_info(tab.page, *point), None
    raise BrowserError("drag needs ref, find or x,y for the start and to_ref, text or to_x,to_y for the end.")


def _check_risky_click(info: dict):
    if policy.confirm_risky():
        deep = info.get("deep") or {}
        risky = policy.risky_label(info.get("label"), info.get("text"), info.get("value"), info.get("title"),
                                   info.get("actionLabel"), info.get("hostLabel"),
                                   deep.get("label"), deep.get("text"), deep.get("value"), deep.get("title"),
                                   deep.get("actionLabel"), deep.get("hostLabel"))
        if risky:
            raise Handoff(f"clicking {_q(risky)} would commit something on your behalf (pay, order, send, "
                          "publish, apply, delete or similar).")


async def _navigate(driver: _Driver, tab: _Tab, key: str, args: dict) -> tuple[str, dict]:
    ok, reason, url = policy.check_url(str(args.get("url") or ""))
    if not ok:
        raise BrowserError(f"Not opened: {reason}")
    timeout = int(args.get("timeout_ms") or 0) or int(float(args.get("timeout_s") or 0) * 1000) or _NAV_TIMEOUT_MS
    timeout = min(max(timeout, 5000), 60000)
    await tab.page.bring_to_front()
    note = f"Opened {url}."
    try:
        response = await tab.page.goto(url, wait_until="domcontentloaded", timeout=timeout)
        if response is not None and response.status >= 400:
            note += f" The server answered HTTP {response.status}."
    except Exception as exc:
        message = _short_error(exc)
        if "Timeout" in message:
            note += " It is still loading (timed out waiting); the state below may be partial."
        else:
            raise BrowserError(f"Could not open {url}: {message}") from None
    await driver.settle(tab.page)
    return await _state_result(driver, tab, key, "open", note)


def parse_batch(args: dict) -> tuple[list[dict], list[str] | None, dict | None]:
    """Validate a batch: (steps, items or None, pages or None). Raises
    BrowserError with a message the model can act on."""
    steps = args.get("steps")
    if not isinstance(steps, list) or not steps:
        raise BrowserError("batch needs steps: a list of actions, for example "
                           '[{"action": "click", "find": "Next"}, {"action": "read"}].')
    if len(steps) > _BATCH_MAX_STEPS:
        raise BrowserError(f"batch takes at most {_BATCH_MAX_STEPS} steps.")
    clean = []
    for number, step in enumerate(steps, 1):
        if not isinstance(step, dict):
            raise BrowserError(f"batch step {number} must be an object with an action.")
        action = str(step.get("action") or "").strip().lower()
        if action not in _BATCH_STEP_ACTIONS:
            raise BrowserError(f"batch step {number}: {_q(action or '(none)')} cannot run in a batch. Allowed: "
                               + ", ".join(sorted(_BATCH_STEP_ACTIONS)) + ". Uploads, screenshots, tabs and "
                               "JavaScript are separate calls.")
        extra = set(step) - _BATCH_STEP_ARGS
        if extra:
            raise BrowserError(f"batch step {number}: unsupported argument(s) {', '.join(sorted(extra))}.")
        for name, value in step.items():
            if isinstance(value, (dict, list)):
                raise BrowserError(f"batch step {number}: {name} must be a plain value.")
            if isinstance(value, str) and ("\0" in value or value.lstrip().startswith("-")):
                raise BrowserError(f"batch step {number}: {name} cannot start with a dash.")
        clean.append({**step, "action": action})
    items = args.get("each")
    pages = args.get("pages")
    if items is not None and pages is not None:
        raise BrowserError("batch takes each or pages, not both.")
    if items is not None:
        if (not isinstance(items, list) or not items or len(items) > _BATCH_MAX_ITEMS
                or not all(isinstance(i, (str, int, float)) and not isinstance(i, bool) for i in items)):
            raise BrowserError(f"each must be a list of 1 to {_BATCH_MAX_ITEMS} short texts; the steps run once "
                               "per item with {item} replaced by it.")
        items = [str(i)[:200] for i in items]
        if any("\0" in i or i.lstrip().startswith("-") for i in items):
            raise BrowserError("each items cannot start with a dash.")
    if pages is not None:
        if not isinstance(pages, dict) or not str(pages.get("next") or "").strip() or set(pages) - {"next", "max"}:
            raise BrowserError('pages needs next (the visible text of the next page link or button) and '
                               'optionally max, for example {"next": "Next", "max": 5}.')
        try:
            limit = int(pages.get("max") or 5)
        except (TypeError, ValueError):
            raise BrowserError("pages.max must be a whole number.") from None
        nxt = str(pages["next"]).strip()[:200]
        if nxt.startswith("-"):
            raise BrowserError("pages.next cannot start with a dash.")
        pages = {"next": nxt, "max": min(max(limit, 1), _BATCH_MAX_PAGES)}
    runs = len(clean) * (len(items) if items else pages["max"] if pages else 1)
    if runs > _BATCH_MAX_RUNS:
        raise BrowserError(f"That batch would run {runs} steps; the limit is {_BATCH_MAX_RUNS}. Split it.")
    return clean, items, pages


def _with_item(step: dict, item: str) -> dict:
    return {k: (v.replace("{item}", item) if isinstance(v, str) else v) for k, v in step.items()}


def _step_note(output: str) -> str:
    """A step's own result without the page outline (the batch ends with one)."""
    text = str(output or "").split(STATE_BEGIN, 1)[0].strip()
    return text or "done."


_FINGERPRINT_JS = "() => location.href + '|' + (document.body ? document.body.innerText.length : 0)"


class _BatchFull(Exception):
    pass


async def _batch(driver: _Driver, args: dict, key: str) -> tuple[str, dict]:
    steps, items, pages = parse_batch(args)
    started = time.monotonic()
    lines: list[str] = []
    used = 0

    def log(label: str, text: str) -> None:
        nonlocal used
        entry = f"[{label}] {text}"
        if used + len(entry) > _BATCH_OUTPUT_CHARS:
            # Stop rather than carry on without telling the model what it missed.
            lines.append(f"[{label}] done, but its output did not fit in this batch's reply.")
            raise _BatchFull(label)
        lines.append(entry)
        used += len(entry) + 1

    def check_time(before: str) -> None:
        if time.monotonic() - started > _BATCH_TIME_S:
            raise BrowserError(f"the batch reached its {_BATCH_TIME_S} s limit before {before}")

    async def act(call: dict, label: str) -> str:
        driver.action_started = time.monotonic()   # wait only for this step's downloads
        try:
            _, result = await asyncio.wait_for(_act(driver, call, key), _ACTION_TIMEOUT_S)
        except (Handoff, asyncio.CancelledError):
            raise
        except BrowserError as error:
            raise BrowserError(f"{label} failed: {error}") from None
        except asyncio.TimeoutError:
            raise BrowserError(f"{label} took longer than {_ACTION_TIMEOUT_S} s") from None
        except Exception as exc:   # Playwright errors: stop, but keep the log of what was done
            raise BrowserError(f"{label} failed: {_short_error(exc)}") from None
        output = str(result.get("output") or "")
        note = _step_note(output)
        downloads = [line for line in output.split(STATE_BEGIN, 1)[-1].splitlines() if line.startswith("Downloaded ")]
        if downloads and STATE_BEGIN in output:
            note += "\n" + "\n".join(downloads)
        if int(result.get("exit_code") or 0) != 0:
            raise BrowserError(f"{label} failed: {note[:600]}")
        return note

    async def run_steps(prefix: str, item: str | None) -> None:
        for number, step in enumerate(steps, 1):
            call = _with_item(step, item) if item is not None else step
            label = f"{prefix}step {number} {call['action']}"
            check_time(label)
            log(label, await act(call, label))

    async def fingerprint() -> str:
        tab = driver._active(key)
        if tab is None:
            return ""
        try:
            return await asyncio.wait_for(tab.page.evaluate(_FINGERPRINT_JS), 5)
        except Exception:
            raise BrowserError("the page stopped responding (a browser dialog may be waiting)") from None

    summary = ""
    try:
        if items is not None:
            for index, item in enumerate(items, 1):
                await run_steps(f"item {index} {_q(item)[:60]}, ", item)
            summary = f"Batch finished: {len(steps)} step(s) for each of {len(items)} item(s)."
        elif pages is not None:
            for number in range(1, pages["max"] + 1):
                await run_steps(f"page {number}, ", None)
                if number == pages["max"]:
                    summary = f"Batch finished: {number} page(s), the most asked for."
                    break
                label = f"page {number}, {_q(pages['next'])}"
                check_time(label)
                before = await fingerprint()
                try:
                    await act({"action": "click", "find": pages["next"]}, label)
                except BrowserError as error:
                    if "No visible element matches" not in str(error):
                        raise   # covered, blocked, a dialog: the data is not complete
                    summary = f"Batch finished: {number} page(s); there is no {_q(pages['next'])} control on the last one."
                    break
                if await fingerprint() == before:
                    summary = f"Batch finished: {number} page(s); {_q(pages['next'])} did not change the page."
                    break
        else:
            await run_steps("", None)
            summary = f"Batch finished: {len(steps)} step(s)."
    except Handoff as handoff:
        text = "\n".join(lines + ["Batch stopped here; the steps above were done.", _handoff_text(str(handoff))])
        return "batch", {"output": text + await _error_state(driver, key), "exit_code": 0}
    except _BatchFull as full:
        summary = (f"Batch stopped after {full}: the reply would have been too long. Run another batch "
                   "from the next item or page to read the rest.")
    except BrowserError as error:
        text = "\n".join(lines + [f"Batch stopped: {error}. The steps above were done."])
        return "batch", {"output": text + await _error_state(driver, key), "exit_code": 1, "error": str(error)}
    tab = driver._active(key)
    if tab is None:
        return "batch", {"output": "\n".join(lines + [summary]), "exit_code": 0}
    what, result = await _state_result(driver, tab, key, "batch", "\n".join(lines + [summary]))
    if int(result.get("exit_code") or 0) != 0:
        result["output"] = "\n".join(lines + [summary, str(result.get("output") or "")])
    return what, result


async def _state_result(driver: _Driver, tab: _Tab, key: str, what: str, note: str) -> tuple[str, dict]:
    blocked = await driver.guard_current_url(tab)
    if blocked:
        return what, {"output": blocked, "exit_code": 1, "error": blocked}
    state, _ = await driver.observe(tab, key, notes=await driver.download_notes())
    return what, {"output": f"{note}\n{state}", "exit_code": 0}


async def _error_state(driver: _Driver, key: str) -> str:
    tab = driver._active(key)
    if tab is None:
        return ""
    try:
        state, _ = await asyncio.wait_for(driver.observe(tab, key), 15)
        return "\n" + state
    except Exception:
        return ""


async def run(args: dict, *, owner: str | None, session_id: str | None,
              delegated: bool = False) -> tuple[str, dict]:
    """Execute one private_browser action. Returns (description, result)."""
    action = str(args.get("action") or "")
    label = f"private_browser: {action}"
    if delegated:
        # API tokens and other delegated credentials never drive the browser
        # that holds the person's logins (same rule as sending email).
        return label, {"output": "The browser is not available to API tokens or delegated credentials.",
                       "exit_code": 1, "error": "browser not available to delegated credentials",
                       "executed": False, "failure_kind": "browser_page_authority_unavailable"}
    if not enabled():
        return label, {"output": "The OpenWings browser is not enabled on this server. It is switched on with "
                                 "./scripts/setup.sh --with-browser. Use web_search and web_fetch instead.",
                       "exit_code": 1, "error": "browser not enabled"}
    if not policy.user_allowed(owner):
        return label, {"output": "The shared browser holds one person's logins and is reserved for "
                                 "the accounts in WINGS_BROWSER_USERS. Use web_search and web_fetch instead.",
                       "exit_code": 1, "error": "browser not allowed for this account"}
    key = f"{str(owner or '').casefold()}:{session_id or 'default'}"
    driver = _DRIVER
    # A batch checks its own limit before each step; the outer limit leaves
    # room for the step in flight and the final page state.
    limit = _BATCH_TIME_S + _ACTION_TIMEOUT_S + 60 if action == "batch" else _ACTION_TIMEOUT_S
    async with driver.lock:
        try:
            await driver.ensure()
            driver.action_started = time.monotonic()
            what, result = await asyncio.wait_for(_act(driver, args, key), limit)
            return f"{label} {what}".strip(), result
        except Handoff as handoff:
            text = _handoff_text(str(handoff))
            return label, {"output": text + await _error_state(driver, key), "exit_code": 0}
        except BrowserError as error:
            text = f"Error: {error}"
            return label, {"output": text + await _error_state(driver, key), "exit_code": 1, "error": str(error)}
        except asyncio.TimeoutError:
            text = f"Error: the {action} action took longer than {limit} s and was abandoned."
            return label, {"output": text + await _error_state(driver, key), "exit_code": 1, "error": text}
        except Exception as exc:  # Playwright errors, lost connection
            logger.warning("browser action %s failed: %s", action, _short_error(exc))
            if not driver.connected():
                text = "Error: the connection to the browser was lost. Try the action again."
            else:
                text = f"Error: {_short_error(exc)}"
            return label, {"output": text + await _error_state(driver, key), "exit_code": 1, "error": text}


async def status() -> dict:
    """Small status payload for the web UI (no page content)."""
    if not enabled():
        return {"enabled": False}
    driver = _DRIVER
    out = {"enabled": True, "live_view": LIVE_VIEW_PATH, "connected": driver.connected(), "tabs": None}
    if driver.connected():
        out["tabs"] = len(driver._live_tabs())
    return out
