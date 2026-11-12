"""Mail with an app password: Gmail, iCloud and Yahoo (patch 0025).

The person types their address and an app password; Connect first logs in to
the provider's IMAP and SMTP servers with it (upstream's own connection
helpers in routes/email/email_helpers.py, so the outbound address policy and
TLS checks apply), then creates the mail account through upstream's own
account creation route function (validation, encryption of the password,
the one-default-per-person rule). Nothing is saved when a login fails.

iCloud: the same app-specific password also opens iCloud Calendar over CalDAV
(caldav.icloud.com); with "Also sync my iCloud calendars" ticked, a CalDAV
account is added the way upstream's calendar settings add one (URL checked by
src/caldav_sync.validate_caldav_url, password encrypted, saved in the
person's preferences) and a first sync starts.

Server names and ports are the providers' published ones:
  Gmail   imap.gmail.com:993 SSL, smtp.gmail.com:465 SSL
          (support.google.com/accounts/answer/185833 for app passwords)
  iCloud  imap.mail.me.com:993 SSL, smtp.mail.me.com:587 STARTTLS
          (support.apple.com/en-us/102525; IMAP user name "usually the name
          of your iCloud Mail email address", else the full address)
  Yahoo   imap.mail.yahoo.com:993 SSL, smtp.mail.yahoo.com:465 SSL
"""
from __future__ import annotations

import asyncio
import imaplib
import logging
import re
import smtplib
import socket
import ssl
import uuid
from typing import Any, Dict, List, Optional, Tuple

from wings.accounts import AccountError, mail_target

logger = logging.getLogger(__name__)

EMAIL_RE = re.compile(r"^[^@\s<>\"']{1,64}@[A-Za-z0-9.-]{1,253}\.[A-Za-z]{2,63}$")
ICLOUD_CALDAV_URL = "https://caldav.icloud.com"
ICLOUD_DOMAINS = ("icloud.com", "me.com", "mac.com")

PROVIDERS: Dict[str, Dict[str, Any]] = {
    "gmail": {
        "name": "Gmail",
        "imap": ("imap.gmail.com", 993, "ssl"),
        "smtp": ("smtp.gmail.com", 465, "ssl"),
        "example": "you@gmail.com",
        "intro": "Google does not let other apps use your normal password. An app password is a 16 letter code made just for OpenWings.",
        "steps": [
            {"text": "Turn on 2-Step Verification in your Google Account.",
             "url": "https://myaccount.google.com/signinoptions/two-step-verification", "link": "Open 2-Step Verification"},
            {"text": "Open App passwords, type OpenWings as the name and press Create.",
             "url": "https://myaccount.google.com/apppasswords", "link": "Open App passwords"},
            {"text": "Copy the 16 letter code and paste it below. Spaces are fine."},
        ],
        "help": "https://support.google.com/accounts/answer/185833",
        "unavailable": ("Google does not offer app passwords on every account: not on most work or school "
                        "accounts, not with Advanced Protection, and not when 2-Step Verification uses only "
                        "security keys. Ask your OpenWings admin to set up Sign in with Google once, or add "
                        "your calendar with Calendar by link."),
    },
    "icloud": {
        "name": "iCloud",
        "imap": ("imap.mail.me.com", 993, "ssl"),
        "smtp": ("smtp.mail.me.com", 587, "starttls"),
        "example": "you@icloud.com",
        "intro": "Apple does not let other apps use your Apple Account password. An app-specific password is made just for OpenWings.",
        "steps": [
            {"text": "Sign in at account.apple.com. Two-factor authentication must be on (it is for most accounts).",
             "url": "https://account.apple.com", "link": "Open Apple Account"},
            {"text": "In Sign-In and Security, choose App-Specific Passwords, then generate one named OpenWings.",
             "url": "https://support.apple.com/en-us/102654", "link": "Apple's steps"},
            {"text": "Paste the password below. Spaces and dashes in it are fine."},
        ],
        "help": "https://support.apple.com/en-us/102654",
        "unavailable": ("Apple only gives app-specific passwords to accounts with two-factor authentication. "
                        "Turn it on first (support.apple.com/en-us/102660), then make the password."),
        "calendar": True,
    },
    "yahoo": {
        "name": "Yahoo",
        "imap": ("imap.mail.yahoo.com", 993, "ssl"),
        "smtp": ("smtp.mail.yahoo.com", 465, "ssl"),
        "example": "you@yahoo.com",
        "intro": "Yahoo does not let other apps use your normal password. An app password is made just for OpenWings.",
        "steps": [
            {"text": "Open Yahoo Account Security.",
             "url": "https://login.yahoo.com/account/security", "link": "Open Account Security"},
            {"text": "Under External connections, choose Create app password, name it OpenWings and press Generate.",
             "url": "https://help.yahoo.com/kb/SLN15241.html", "link": "Yahoo's steps"},
            {"text": "Paste the password below."},
        ],
        "help": "https://help.yahoo.com/kb/SLN15241.html",
        "unavailable": "If Yahoo offers no app password, sign in to Yahoo in a browser you use often (not a private window) and try again.",
    },
}


def provider_cards() -> List[Dict[str, Any]]:
    out = []
    for key, p in PROVIDERS.items():
        out.append({"key": key, "name": p["name"], "example": p["example"], "intro": p["intro"],
                    "steps": p["steps"], "help": p["help"], "calendar": bool(p.get("calendar")),
                    "unavailable": p.get("unavailable", "")})
    return out


def clean_password(raw: str) -> str:
    """App passwords are shown in groups; spaces (and Apple's dashes are part
    of the password, so they stay) are dropped."""
    return re.sub(r"\s+", "", raw or "")


_DOMAINS = {
    "gmail": ("gmail.com", "googlemail.com"),
    "icloud": ICLOUD_DOMAINS,
    "yahoo": ("yahoo.com", "ymail.com", "rocketmail.com"),
}


def provider_for_host(host: str, address: str = "") -> str:
    """Which card a mail account belongs to: by its IMAP server, else (a
    manual setup, or a test server) by the address's domain."""
    host = (host or "").lower()
    for key, p in PROVIDERS.items():
        if host == p["imap"][0]:
            return key
    if host in ("outlook.office365.com",):
        return "outlook"
    domain = (address or "").rpartition("@")[2].lower()
    for key, domains in _DOMAINS.items():
        if domain in domains or (key == "yahoo" and domain.startswith("yahoo.")):
            return key
    return "other"


# ------------------------------------------------------------------ login test

class LoginFailed(AccountError):
    pass


def _imap_users(provider_key: str, address: str) -> List[str]:
    users = [address]
    local, _, domain = address.partition("@")
    if provider_key == "icloud" and domain.lower() in ICLOUD_DOMAINS:
        users.append(local)
    return users


def _auth_message(provider_key: str, raw: str) -> str:
    p = PROVIDERS[provider_key]
    low = (raw or "").lower()
    if "application-specific password required" in low or "app password" in low or "web login required" in low:
        return f"{p['name']} wants an app password here, not your normal password. Follow the steps above to make one."
    if any(k in low for k in ("authenticationfailed", "invalid credentials", "authentication failed",
                               "username and password not accepted", "535", "login failed", "auth", "denied")):
        return (f"{p['name']} did not accept that password. Check that you pasted the app password "
                f"(not your normal password) and the right address.")
    return f"{p['name']} refused the sign-in: {raw[:160]}" if raw else f"{p['name']} refused the sign-in."


def _imap_login(provider_key: str, address: str, password: str, owner: str) -> str:
    from routes.email.email_helpers import (
        _IMAP_TIMEOUT_SECONDS, _mail_connection_test_error, _open_imap_connection,
    )
    host, port, security = mail_target(*PROVIDERS[provider_key]["imap"])
    last = ""
    for user in _imap_users(provider_key, address):
        try:
            conn = _open_imap_connection(host, port, starttls=(security == "starttls"),
                                         timeout=_IMAP_TIMEOUT_SECONDS, owner=owner)
        except Exception as exc:
            raise LoginFailed("imap_unreachable", f"Could not reach {PROVIDERS[provider_key]['name']} mail: "
                              + _mail_connection_test_error("IMAP", host, exc)) from None
        try:
            conn.login(user, password)
            return user
        except imaplib.IMAP4.error as exc:
            last = str(exc)
        finally:
            try:
                conn.logout()
            except Exception:
                pass
    raise LoginFailed("wrong_password", _auth_message(provider_key, last))


def _smtp_login(provider_key: str, address: str, password: str, owner: str) -> None:
    from routes.email.email_helpers import (
        _PolicySMTP, _PolicySMTP_SSL, _mail_connection_test_error, _mail_private_blocked,
    )
    host, port, security = mail_target(*PROVIDERS[provider_key]["smtp"])
    block_private = _mail_private_blocked(owner)
    smtp = None
    try:
        if security == "ssl":
            smtp = _PolicySMTP_SSL(host, port, timeout=15, block_private=block_private)
        else:
            smtp = _PolicySMTP(host, port, timeout=15, block_private=block_private)
            if security == "starttls":
                smtp.starttls(context=ssl.create_default_context())
        smtp.login(address, password)
    except smtplib.SMTPAuthenticationError as exc:
        raw = exc.smtp_error.decode("utf-8", "replace") if isinstance(exc.smtp_error, bytes) else str(exc.smtp_error)
        raise LoginFailed("wrong_password", _auth_message(provider_key, f"{exc.smtp_code} {raw}")) from None
    except smtplib.SMTPNotSupportedError:
        raise LoginFailed("smtp_failed", f"{PROVIDERS[provider_key]['name']} sending did not offer a sign-in.") from None
    except (OSError, smtplib.SMTPException) as exc:
        raise LoginFailed("smtp_unreachable", f"Could not reach {PROVIDERS[provider_key]['name']} sending: "
                          + _mail_connection_test_error("SMTP", host, exc)) from None
    finally:
        if smtp is not None:
            try:
                smtp.quit()
            except Exception:
                try:
                    smtp.close()
                except Exception:
                    pass


def test_login(provider_key: str, address: str, password: str, owner: str) -> Dict[str, Any]:
    """Log in to IMAP and SMTP; returns the account settings that worked."""
    if provider_key not in PROVIDERS:
        raise AccountError("unknown_provider", "Pick Gmail, iCloud or Yahoo.")
    address = (address or "").strip()
    if not EMAIL_RE.match(address):
        raise AccountError("bad_address", "Type your whole email address, for example " + PROVIDERS[provider_key]["example"] + ".")
    password = clean_password(password)
    if not password:
        raise AccountError("no_password", "Paste the app password.")
    if len(password) > 256:
        raise AccountError("bad_password", "That does not look like an app password.")
    imap_user = _imap_login(provider_key, address, password, owner)
    _smtp_login(provider_key, address, password, owner)
    ihost, iport, isec = mail_target(*PROVIDERS[provider_key]["imap"])
    shost, sport, ssec = mail_target(*PROVIDERS[provider_key]["smtp"])
    return {
        "name": address,
        "imap_host": ihost, "imap_port": iport, "imap_user": imap_user,
        "imap_password": password, "imap_starttls": isec == "starttls",
        "smtp_host": shost, "smtp_port": sport, "smtp_security": ssec,
        "smtp_user": address, "smtp_password": password,
        "from_address": address,
    }


# ------------------------------------------------------------------ upstream hooks

def route_endpoint(request, path: str, method: str):
    from wings.accounts import route_endpoint as _find
    return _find(request, path, method)


def existing_account_id(owner: str, address: str) -> Optional[str]:
    from core.database import EmailAccount, SessionLocal
    from sqlalchemy import func, or_
    db = SessionLocal()
    try:
        row = (db.query(EmailAccount)
               .filter(EmailAccount.owner == owner,
                       or_(func.lower(EmailAccount.from_address) == address.lower(),
                           func.lower(EmailAccount.smtp_user) == address.lower()))
               .first())
        return row.id if row is not None else None
    finally:
        db.close()


async def save_account(request, owner: str, data: Dict[str, Any]) -> str:
    """Create (or update) the mail account with upstream's own route code."""
    existing = existing_account_id(owner, data["from_address"])
    if existing:
        update = route_endpoint(request, "/api/email/accounts/{account_id}", "PUT")
        if update is None:
            raise AccountError("save_failed", "The mail account could not be saved.")
        body = dict(data)
        body["enabled"] = True
        result = await update(account_id=existing, data=body, owner=owner, request=request)
        if not (isinstance(result, dict) and result.get("ok")):
            raise AccountError("save_failed", "The mail account could not be saved.")
        _clear_oauth(existing)
        return existing
    create = route_endpoint(request, "/api/email/accounts", "POST")
    if create is None:
        raise AccountError("save_failed", "The mail account could not be saved.")
    result = await create(data=dict(data), owner=owner, request=request)
    if not (isinstance(result, dict) and result.get("ok")):
        raise AccountError("save_failed", "The mail account could not be saved: " + str((result or {}).get("error") or "")[:120])
    return result["id"]


def _clear_oauth(account_id: str) -> None:
    """A password now signs this account in; drop an old OAuth sign-in."""
    from core.database import EmailAccount, SessionLocal
    db = SessionLocal()
    try:
        row = db.get(EmailAccount, account_id)
        if row is not None and row.oauth_provider:
            row.oauth_provider = None
            row.oauth_access_token = None
            row.oauth_refresh_token = None
            row.oauth_token_expiry = None
            db.commit()
    finally:
        db.close()


def add_caldav_account(owner: str, *, label: str, url: str, username: str, password: str = "",
                       oauth: Optional[Dict[str, str]] = None) -> str:
    """Add (or update) a CalDAV account the way upstream's calendar settings
    do; with `oauth`, the account signs in with a bearer token from
    wings.accounts instead of a password."""
    from routes.prefs_routes import _load_for_user, _save_for_user
    from src.caldav_sync import _load_caldav_accounts, validate_caldav_url
    from src.secret_storage import encrypt
    try:
        url = validate_caldav_url(url)
    except ValueError as exc:
        raise AccountError("bad_caldav", str(exc)) from None
    accounts = _load_caldav_accounts(owner)
    acc = next((a for a in accounts if (a.get("url") or "").rstrip("/") == url.rstrip("/")
                and (a.get("username") or "").lower() == username.lower()), None)
    if acc is None:
        acc = {"id": str(uuid.uuid4())}
        accounts.append(acc)
    acc["label"] = label
    acc["url"] = url
    acc["username"] = username
    if oauth:
        acc["password"] = ""
        acc["wings_oauth"] = dict(oauth)
    else:
        acc["password"] = encrypt(password)
        acc.pop("wings_oauth", None)
    prefs = _load_for_user(owner) or {}
    prefs["caldav_accounts"] = accounts
    prefs.pop("caldav", None)
    _save_for_user(owner, prefs)
    return acc["id"]


def remove_caldav_account(owner: str, account_id: str) -> bool:
    """Forget a CalDAV account and the calendars it brought in."""
    from routes.prefs_routes import _load_for_user, _save_for_user
    from src.caldav_sync import _load_caldav_accounts
    accounts = _load_caldav_accounts(owner)
    kept = [a for a in accounts if a.get("id") != account_id]
    if len(kept) == len(accounts):
        return False
    prefs = _load_for_user(owner) or {}
    prefs["caldav_accounts"] = kept
    prefs.pop("caldav", None)
    _save_for_user(owner, prefs)
    from core.database import CalendarCal, CalendarEvent, SessionLocal
    db = SessionLocal()
    try:
        cals = db.query(CalendarCal).filter(CalendarCal.owner == owner, CalendarCal.source == "caldav",
                                            CalendarCal.account_id == account_id).all()
        for cal in cals:
            db.query(CalendarEvent).filter(CalendarEvent.calendar_id == cal.id).delete()
            db.delete(cal)
        db.commit()
    finally:
        db.close()
    return True


def caldav_accounts(owner: str) -> List[Dict[str, Any]]:
    from src.caldav_sync import _load_caldav_accounts
    out = []
    for a in _load_caldav_accounts(owner):
        url = a.get("url") or ""
        kind = "google" if (a.get("wings_oauth") or {}).get("provider") == "google" else (
            "icloud" if "icloud.com" in url else "other")
        out.append({"id": a.get("id"), "label": a.get("label") or "Calendar", "username": a.get("username") or "",
                    "kind": kind})
    return out


def start_sync(owner: str) -> None:
    """Pull the person's CalDAV calendars now, without making them wait."""
    async def _run():
        try:
            from src.caldav_sync import sync_caldav
            result = await sync_caldav(owner)
            if result.get("errors"):
                logger.info("Calendar sync after connect: %d error(s)", len(result["errors"]))
        except Exception as exc:
            logger.warning("Calendar sync after connect failed: %s", type(exc).__name__)
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    task = loop.create_task(_run())
    _BG.add(task)
    task.add_done_callback(_BG.discard)


_BG: set = set()


async def connect(request, owner: str, provider_key: str, address: str, password: str, *,
                  calendar: bool = False) -> Dict[str, Any]:
    from starlette.concurrency import run_in_threadpool
    data = await run_in_threadpool(test_login, provider_key, address, password, owner)
    account_id = await save_account(request, owner, data)
    out: Dict[str, Any] = {"ok": True, "account_id": account_id, "address": data["from_address"]}
    if calendar and PROVIDERS[provider_key].get("calendar"):
        try:
            out["caldav_account_id"] = add_caldav_account(
                owner, label=f"iCloud Calendar ({data['from_address']})", url=ICLOUD_CALDAV_URL,
                username=data["from_address"], password=data["imap_password"])
            start_sync(owner)
            out["calendar"] = "syncing"
        except AccountError as exc:
            out["calendar"] = "failed"
            out["calendar_error"] = exc.message
    return out


def mail_accounts(owner: str) -> List[Dict[str, Any]]:
    from core.database import EmailAccount, SessionLocal
    db = SessionLocal()
    try:
        rows = db.query(EmailAccount).filter(EmailAccount.owner == owner).order_by(EmailAccount.created_at).all()
        return [{
            "id": r.id,
            "address": r.from_address or r.imap_user or r.name,
            "provider": "google_oauth" if r.oauth_provider == "google" else (
                r.oauth_provider if r.oauth_provider else provider_for_host(r.imap_host, r.from_address or r.imap_user)),
            "oauth": r.oauth_provider or "",
            "enabled": bool(r.enabled),
        } for r in rows]
    finally:
        db.close()
