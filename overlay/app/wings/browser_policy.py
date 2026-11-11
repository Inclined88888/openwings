"""Safety rules for OpenWings browser automation. Pure functions, no I/O apart
from DNS lookups, so they can be unit tested without a browser.

What the agent may NOT do on its own, whatever the page or prompt says:
  * type into password, one-time-code or payment-card fields (the person
    signs in or pays through the live view; credentials never pass through
    the model or the chat history),
  * click buttons that pay, order, send, publish, delete an account or submit
    an application (handed to the person unless WINGS_BROWSER_CONFIRM_RISKY=0),
  * open non-web URLs (file:, chrome:, data:, javascript:) or addresses on
    your private network (unless WINGS_BROWSER_ALLOW_PRIVATE=1),
  * run its own JavaScript in pages (unless WINGS_BROWSER_ALLOW_EVAL=1).
"""
from __future__ import annotations

import ipaddress
import os
import re
import socket
from urllib.parse import urlsplit, urlunsplit


def _flag(name: str, default: bool) -> bool:
    value = os.environ.get(name, "").strip().lower()
    if value in ("1", "true", "yes", "on"):
        return True
    if value in ("0", "false", "no", "off"):
        return False
    return default


def allow_private() -> bool:
    return _flag("WINGS_BROWSER_ALLOW_PRIVATE", False)


def confirm_risky() -> bool:
    return _flag("WINGS_BROWSER_CONFIRM_RISKY", True)


def allow_eval() -> bool:
    return _flag("WINGS_BROWSER_ALLOW_EVAL", False)


def allowed_domains() -> list[str]:
    raw = os.environ.get("WINGS_BROWSER_ALLOWED_DOMAINS", "")
    return [d.strip().lower().lstrip(".") for d in raw.split(",") if d.strip()]


def allowed_users() -> set[str]:
    """OpenWings accounts that may use the shared browser. One browser profile
    holds one person's logins, so by default only the admin account."""
    raw = os.environ.get("WINGS_BROWSER_USERS", "").strip()
    if not raw:
        raw = os.environ.get("ODYSSEUS_ADMIN_USER", "admin") or "admin"
    return {u.strip().casefold() for u in raw.split(",") if u.strip()}


def user_allowed(username: str | None) -> bool:
    users = allowed_users()
    return "*" in users or (bool(username) and str(username).casefold() in users)


# --------------------------------------------------------------------------
# URLs
# --------------------------------------------------------------------------

_BARE_HOST = re.compile(r"^(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}(?::\d{1,5})?(?:[/?#].*)?$", re.I)


def normalize_url(raw: str) -> str:
    url = str(raw or "").strip()
    if not url:
        return url
    if url.lower() == "about:blank":
        return "about:blank"
    if "://" not in url and _BARE_HOST.match(url):
        url = "https://" + url
    return url


_NAT64 = ipaddress.ip_network("64:ff9b::/96")


def ip_is_public(ip: str) -> bool:
    """Globally routable unicast, after unwrapping IPv4 embedded in IPv6
    (mapped, 6to4, NAT64), so ::ffff:10.0.0.1 or 64:ff9b::a9fe:a9fe count as private."""
    try:
        addr = ipaddress.ip_address(ip.split("%", 1)[0])
    except ValueError:
        return False
    if addr.version == 6:
        if addr.ipv4_mapped is not None:
            addr = addr.ipv4_mapped
        elif addr.sixtofour is not None:
            addr = addr.sixtofour
        elif addr in _NAT64:
            addr = ipaddress.ip_address(int(addr) & 0xFFFFFFFF)
    return bool(addr.is_global) and not addr.is_multicast


def _domain_allowed(host: str, domains: list[str]) -> bool:
    if not domains:
        return True
    host = host.lower().rstrip(".")
    return any(host == d or host.endswith("." + d) for d in domains)


def check_url(raw: str, *, private_ok: bool | None = None, domains: list[str] | None = None,
              resolver=socket.getaddrinfo) -> tuple[bool, str, str]:
    """Return (allowed, reason, normalized_url)."""
    url = normalize_url(raw)
    if url == "about:blank":
        return True, "", url
    if not url:
        return False, "No URL given.", url
    try:
        parts = urlsplit(url)
    except ValueError:
        return False, "That is not a valid URL.", url
    scheme = (parts.scheme or "").lower()
    if scheme not in ("http", "https"):
        return False, f"Only http and https pages can be opened (got '{scheme or 'none'}:').", url
    if parts.username or parts.password:
        return False, "URLs with embedded usernames or passwords are refused. Sign in through the live view instead.", url
    host = (parts.hostname or "").rstrip(".").lower()
    if not host:
        return False, "The URL has no host name.", url
    if private_ok is None:
        private_ok = allow_private()
    if domains is None:
        domains = allowed_domains()
    if not _domain_allowed(host, domains):
        return False, f"{host} is not in WINGS_BROWSER_ALLOWED_DOMAINS.", url
    if not private_ok:
        literal = None
        try:
            literal = ipaddress.ip_address(host.strip("[]"))
        except ValueError:
            pass
        if literal is not None:
            if not ip_is_public(str(literal)):
                return False, f"{host} is a private or local address. Set WINGS_BROWSER_ALLOW_PRIVATE=1 to allow it.", url
        else:
            if "." not in host or host.endswith((".local", ".internal", ".localhost", ".lan", ".home.arpa")) or host == "localhost":
                return False, f"{host} is a local network name. Set WINGS_BROWSER_ALLOW_PRIVATE=1 to allow it.", url
            try:
                port = parts.port or (443 if scheme == "https" else 80)
            except ValueError:
                return False, "The URL has an invalid port.", url
            try:
                infos = resolver(host, port, proto=socket.IPPROTO_TCP)
            except (OSError, UnicodeError):
                return False, f"Could not resolve {host}.", url
            addresses = {info[4][0] for info in infos}
            if not addresses:
                return False, f"Could not resolve {host}.", url
            bad = sorted(a for a in addresses if not ip_is_public(a))
            if bad:
                return False, f"{host} resolves to a private or local address ({bad[0]}). Set WINGS_BROWSER_ALLOW_PRIVATE=1 to allow it.", url
    # Drop any fragment-only weirdness but keep the URL as the model gave it.
    return True, "", urlunsplit((scheme, parts.netloc, parts.path, parts.query, parts.fragment))


# --------------------------------------------------------------------------
# Risky actions and sensitive fields
# --------------------------------------------------------------------------

_RISKY_PATTERNS = [
    r"pay(?:\s+now|\s+securely|\s+with\b.*)?",
    r"place\s+(?:your\s+|my\s+)?order",
    r"(?:complete|confirm|submit|finali[sz]e)\s+(?:the\s+|my\s+|your\s+)?(?:order|purchase|payment|booking|reservation|transfer|application|transaction)",
    r"buy(?:\s+now|\s+it\s+now)?",
    r"purchase(?:\s+now)?",
    r"subscribe(?:\s+now)?",
    r"start\s+(?:my\s+|your\s+)?(?:paid\s+)?(?:subscription|membership|plan)",
    r"(?:send|transfer)\s+(?:money|funds|payment)",
    r"transfer",
    r"donate(?:\s+now)?",
    r"book(?:\s+now|\s+and\s+pay)",
    r"reserve\s+(?:now|and\s+pay)",
    r"send(?:\s+(?:now|message|email|mail|reply|invoice|it))?",
    r"post(?:\s+(?:now|reply|comment|it))?",
    r"publish(?:\s+now)?",
    r"tweet",
    r"submit\s+(?:my\s+|your\s+|the\s+)?application",
    r"sign\s+(?:and\s+submit|the\s+(?:contract|agreement|document))",
    r"(?:delete|close|deactivate|cancel)\s+(?:my\s+|your\s+|the\s+)?account",
    r"(?:permanently\s+)?delete(?:\s+forever|\s+permanently)?",
    r"unsubscribe",
    r"cancel\s+(?:my\s+|your\s+|the\s+)?(?:subscription|membership|order|plan|booking)",
    r"accept\s+(?:offer|terms\s+and\s+pay)",
    r"withdraw",
]
# Whole label is the phrase, optionally followed by a price or other trailing
# segment that starts with a non word character ("Pay • $42.10", "Buy now (2)").
_RISKY_RE = re.compile(r"^\s*(?:" + "|".join(_RISKY_PATTERNS) + r")\s*(?:[^\w\s].*)?$", re.I | re.S)
# Long labels: a risky phrase as the leading words, e.g. "Pay $42.10 now".
_RISKY_PREFIX_RE = re.compile(
    r"^\s*(?:pay|place\s+(?:your\s+)?order|buy\s+now|purchase|complete\s+(?:purchase|order|payment)|confirm\s+(?:and\s+pay|payment|purchase|order)|send\s+(?:money|payment)|donate|delete\s+(?:my\s+)?account)\b",
    re.I,
)


# Unambiguous commit phrases, matched anywhere in a label or a dialog text.
_RISKY_ANYWHERE_RE = re.compile(
    r"\b(?:pay\s+now|place\s+(?:your\s+|my\s+|the\s+|this\s+)?order|buy\s+(?:it\s+)?now|complete\s+(?:your\s+|my\s+)?(?:purchase|order|payment)"
    r"|confirm\s+(?:and\s+pay|(?:your\s+|my\s+|the\s+)?(?:payment|purchase|order|booking|transfer))"
    r"|submit\s+(?:your\s+|my\s+|the\s+)?(?:order|payment|application)|send\s+(?:money|payment|funds)"
    r"|transfer\s+(?:money|funds)|(?:delete|close|deactivate)\s+(?:your\s+|my\s+|this\s+|the\s+)?account"
    r"|(?:permanently|irreversibly)\s+delete|make\s+(?:a\s+)?payment|authori[sz]e\s+(?:the\s+)?payment"
    r"|start\s+(?:your\s+|my\s+)?(?:paid\s+)?subscription)\b",
    re.I,
)


def risky_label(*labels: str) -> str | None:
    """The first label that names a commit-style action, else None."""
    for label in labels:
        text = re.sub(r"\s+", " ", str(label or "")).strip()
        if not text:
            continue
        if len(text) <= 120 and (_RISKY_RE.match(text) or _RISKY_PREFIX_RE.match(text)):
            return text
        if _RISKY_ANYWHERE_RE.search(text[:300]):
            return text[:120]
    return None


_CONFIRM_RE = re.compile(r"\b(?:are\s+you\s+sure|confirm|proceed|continue)\b", re.I)
_COMMIT_WORD_RE = re.compile(r"\b(?:pay|payment|purchase|order|buy|send|post|publish|delete|remove\s+(?:your|my|this)\s+account"
                             r"|transfer|submit|book|subscribe|unsubscribe|cancel\s+(?:your|my|the)\s+(?:order|subscription|booking))\b",
                             re.I)


def risky_text(text: str) -> bool:
    """Whether a dialog's message asks to confirm a committing action."""
    value = re.sub(r"\s+", " ", str(text or ""))
    return bool(risky_label(value) or (_CONFIRM_RE.search(value) and _COMMIT_WORD_RE.search(value)))


_CARD_HINT = re.compile(r"(?:^|[^a-z])(?:cc-?(?:number|csc|exp)|card.?(?:number|no)|cardnumber|cvv|cvc|csc|security.?code|expir)", re.I)
_OTP_HINT = re.compile(r"one-time-code|\botp\b|verification.?code|2fa|two.?factor|auth(?:entication)?.?code", re.I)


def sensitive_field(info: dict) -> str | None:
    """Why the agent must not type into this field, or None."""
    kind = str(info.get("type") or "").lower()
    autocomplete = str(info.get("autocomplete") or "").lower()
    hints = " ".join(str(info.get(k) or "") for k in ("name", "id", "autocomplete", "label", "placeholder"))
    if kind == "password" or "current-password" in autocomplete or "new-password" in autocomplete:
        return "password"
    if autocomplete.startswith("cc-") or _CARD_HINT.search(hints):
        return "payment card"
    if "one-time-code" in autocomplete or _OTP_HINT.search(hints):
        return "one-time code"
    return None


# --------------------------------------------------------------------------
# Keys
# --------------------------------------------------------------------------

_NAMED_KEYS = {
    "enter": "Enter", "return": "Enter", "tab": "Tab", "escape": "Escape", "esc": "Escape",
    "backspace": "Backspace", "delete": "Delete", "del": "Delete", "space": " ", "spacebar": " ",
    "arrowdown": "ArrowDown", "down": "ArrowDown", "arrowup": "ArrowUp", "up": "ArrowUp",
    "arrowleft": "ArrowLeft", "left": "ArrowLeft", "arrowright": "ArrowRight", "right": "ArrowRight",
    "pagedown": "PageDown", "pageup": "PageUp", "home": "Home", "end": "End", "insert": "Insert",
    **{f"f{n}": f"F{n}" for n in range(1, 13)},
}
_MODIFIERS = {"control": "Control", "ctrl": "Control", "shift": "Shift", "alt": "Alt", "option": "Alt",
              "meta": "Meta", "cmd": "Meta", "command": "Meta", "controlormeta": "ControlOrMeta"}


def is_paste_key(normalized: str) -> bool:
    """Ctrl/Cmd+V, Ctrl+Shift+V or Shift+Insert: pasting whatever is on the
    browser's clipboard (which may be text the person sent from the live view)."""
    parts = normalized.split("+") if normalized != "+" else ["+"]
    base, mods = parts[-1], set(parts[:-1])
    if base.lower() == "v" and mods & {"Control", "Meta", "ControlOrMeta"}:
        return True
    return base == "Insert" and "Shift" in mods


def normalize_key(raw: str) -> str | None:
    """Playwright key string for a key or combination like "Control+A", or None."""
    parts = [p.strip() for p in str(raw or "").replace(" + ", "+").split("+")]
    if not parts or any(not p for p in parts) or len(parts) > 4:
        return None
    *mods, key = parts
    out = []
    for mod in mods:
        name = _MODIFIERS.get(mod.lower())
        if not name:
            return None
        out.append(name)
    named = _NAMED_KEYS.get(key.lower())
    if named:
        out.append(named)
    elif len(key) == 1 and key.isprintable():
        out.append(key)
    else:
        return None
    return "+".join(out)
