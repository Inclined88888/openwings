"""Your own Google key (patch 0025): Sign in with Google for Gmail, Google
Calendar (with Meet links) and Google Drive, free.

Google charges for the yearly security review that a shared, one-click Gmail
sign-in would need, and its API terms do not allow shipping a client secret
in open source. So OpenWings has no Google client of its own: an admin
creates a free Google Cloud OAuth client once (the way Home Assistant, n8n,
Nextcloud and rclone do) and pastes the Client ID and secret here. They are
stored encrypted, install wide (wings/accounts.py). Upstream's .env setup
(GOOGLE_OAUTH_CLIENT_ID / _SECRET / _REDIRECT_URI) keeps working and shows as
"set in .env"; a client saved here wins.

Then every person gets "Sign in with Google" with ticks for Mail, Calendar
and Drive; only the ticked scopes are asked for, with include_granted_scopes
(a later sign-in adds to what was granted), access_type=offline, PKCE (S256)
and a state bound to that person (wings/accounts.py flows).

* One callback for every Google sign-in: upstream's
  /api/email/oauth/google/callback (patched to call `callback` here), so the
  admin registers one redirect URI and a .env setup that registered it keeps
  working. Upstream's own "Connect with Google" for an existing mail account
  (its /api/email/oauth/google/authorize) now runs through the same flow and
  gains PKCE, single use and the ten minute limit.
* Mail (https://mail.google.com/, the scope Gmail IMAP and SMTP need): an
  upstream mail account with oauth_provider "google" (IMAP/SMTP XOAUTH2).
* Calendar (https://www.googleapis.com/auth/calendar): the person's main
  (primary) Google calendar, through upstream's CalDAV sync against Google's
  CalDAV endpoint
  https://apidata.googleusercontent.com/caldav/v2/<calendar id>/events, with
  an OAuth bearer token (Google's CalDAV guide: HTTPS and OAuth 2.0 only;
  the CalDAV API uses the Calendar API scopes). Two-way through upstream's
  sync and write-back. "Add a Google Meet link" patches the event through
  the Calendar API (conferenceDataVersion=1, conferenceData.createRequest
  with conferenceSolutionKey.type "hangoutsMeet") and saves the join link on
  the event.
* Drive (https://www.googleapis.com/auth/drive.readonly): two assistant
  tools, google_drive_search and google_drive_read, for that person only.
  Google Docs and Slides are exported as text, Sheets as CSV; PDFs and text
  files go through upstream's attachment text reader; 10 MB at most.

Google's pages these follow: developers.google.com/identity/protocols/oauth2/
web-server (parameters, redirect URI rules, revoke), /workspace/calendar/
caldav/v2/guide, /workspace/calendar/api/auth, /workspace/calendar/api/
guides/create-events (conferencing), /workspace/drive/api/guides/
manage-downloads and ref-export-formats.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import tempfile
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote, unquote, urlencode, urlsplit

from wings import accounts as A
from wings.accounts import AccountError, AccountTool, Client, Provider

logger = logging.getLogger(__name__)

PROVIDER = "google"
CALLBACK_PATH = "/api/email/oauth/google/callback"
AUTHORIZE_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"
CALDAV_BASE = "https://apidata.googleusercontent.com/caldav/v2/"
CALENDAR_API = "https://www.googleapis.com/calendar/v3"
DRIVE_API = "https://www.googleapis.com/drive/v3"

SCOPE_MAIL = "https://mail.google.com/"
SCOPE_CALENDAR = "https://www.googleapis.com/auth/calendar"
SCOPE_DRIVE = "https://www.googleapis.com/auth/drive.readonly"
BASE_SCOPES = ("openid", "email")
FEATURE_SCOPES = {"mail": SCOPE_MAIL, "calendar": SCOPE_CALENDAR, "drive": SCOPE_DRIVE}

GMAIL_IMAP = ("imap.gmail.com", 993, "ssl")
GMAIL_SMTP = ("smtp.gmail.com", 587, "starttls")
# A Google token only ever goes to Google's own mail servers (wings.accounts
# mail_token checks every account against these before signing in).
A.MAIL_TARGETS[PROVIDER] = [GMAIL_IMAP, GMAIL_SMTP, ("smtp.gmail.com", 465, "ssl")]

MAX_FILE_BYTES = 10 * 1024 * 1024
FILE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,200}$")

# Console pages for the admin's one-time setup (Google's Workspace guides and
# Home Assistant's Google client steps).
CONSOLE = {
    "project": "https://console.cloud.google.com/projectcreate",
    "gmail_api": "https://console.cloud.google.com/apis/enableflow;apiid=gmail.googleapis.com",
    "calendar_api": "https://console.cloud.google.com/apis/enableflow;apiid=calendar-json.googleapis.com",
    "caldav_api": "https://console.cloud.google.com/apis/enableflow;apiid=caldav.googleapis.com",
    "drive_api": "https://console.cloud.google.com/apis/enableflow;apiid=drive.googleapis.com",
    "branding": "https://console.cloud.google.com/auth/branding",
    "audience": "https://console.cloud.google.com/auth/audience",
    "clients": "https://console.cloud.google.com/auth/clients",
    "guide": "https://developers.google.com/workspace/guides/create-credentials",
}


def _env_client() -> Optional[Client]:
    cid = os.environ.get("GOOGLE_OAUTH_CLIENT_ID", "").strip()
    if not cid:
        return None
    return Client(cid, os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET", "").strip() or None,
                  os.environ.get("GOOGLE_OAUTH_REDIRECT_URI", "").strip() or None, "env")


A.register_provider(Provider(
    name=PROVIDER, label="Google", authorize_url=AUTHORIZE_URL, token_url=TOKEN_URL,
    callback_path=CALLBACK_PATH, revoke_url=REVOKE_URL, userinfo_url=USERINFO_URL,
    authorize_extra=(("access_type", "offline"), ("include_granted_scopes", "true"), ("prompt", "consent")),
    env_client=_env_client,
))


def client() -> Optional[Client]:
    try:
        return A.get_client(PROVIDER)
    except Exception:
        logger.warning("Google client store unavailable; using .env only")
        return _env_client()


def client_credentials() -> Tuple[str, str]:
    """(client id, secret) for upstream's Google token refresh."""
    c = client()
    return (c.client_id, c.client_secret or "") if c else ("", "")


REDIRECT_RULES = ("Google accepts only an https address with a public domain name (not .local, .lan or a "
                  "bare IP address), or http on localhost or 127.0.0.1.")


def computed_redirect_uri(request) -> str:
    """The redirect URI for the address this browser uses now (ignoring a
    saved one): the trusted request origin when Google would accept it,
    else the configured base address."""
    origin = A.origin_for(request)
    if not A.safe_redirect_uri(origin + CALLBACK_PATH):
        from wings.origin import configured_base
        origin = configured_base()
    return origin + CALLBACK_PATH


def redirect_uri_for(request) -> str:
    """The exact redirect URI a sign-in uses (and the wizard shows)."""
    c = client()
    if c is not None and c.redirect_uri:
        return c.redirect_uri
    return computed_redirect_uri(request)


def _only_this_computer(request, uri: str) -> bool:
    """True when sign-ins can only finish in a browser on the OpenWings
    computer itself: the person opened OpenWings at a plain http address of
    the network (a phone or another computer cannot use a localhost
    callback)."""
    from wings.origin import request_origin
    here = urlsplit(request_origin(request) or "")
    target = urlsplit(uri or "")
    if not here.hostname or here.scheme == "https":
        return False
    return not A.is_loopback_host(here.hostname) and A.is_loopback_host(target.hostname or "")


def setup_info(request) -> Dict[str, Any]:
    status = A.client_status(PROVIDER)
    uri = redirect_uri_for(request)
    now = computed_redirect_uri(request)
    return {
        "client": status,
        "redirect_uri": uri,
        "redirect_ok": A.safe_redirect_uri(uri),
        "redirect_rules": REDIRECT_RULES,
        "only_this_computer": _only_this_computer(request, uri),
        # A key saved earlier keeps its address; when OpenWings is now opened
        # at another address Google would accept, the wizard offers it.
        "current_uri": now if (status.get("source") == "stored" and now != uri and A.safe_redirect_uri(now)) else "",
        "console": CONSOLE,
    }


def save_setup(request, user: str, client_id: str, client_secret: str) -> Dict[str, Any]:
    uri = redirect_uri_for(request)
    current = client()
    if current is not None and current.source == "stored" and current.redirect_uri:
        uri = current.redirect_uri
    elif current is not None and current.source == "env" and current.redirect_uri:
        uri = current.redirect_uri
    if not A.safe_redirect_uri(uri):
        raise AccountError("bad_redirect", REDIRECT_RULES + " Open OpenWings at such an address first.")
    A.save_client(PROVIDER, client_id, client_secret, uri, by=user)
    return setup_info(request)


def use_current_redirect(request, user: str) -> Dict[str, Any]:
    """Point the saved key at the address OpenWings is opened at now (the
    admin adds it in Google's console too)."""
    current = client()
    if current is None or current.source != "stored":
        raise AccountError("no_client", "There is no saved Google key to change.")
    uri = computed_redirect_uri(request)
    if not A.safe_redirect_uri(uri):
        raise AccountError("bad_redirect", REDIRECT_RULES)
    A.update_client_redirect(PROVIDER, uri, by=user)
    return setup_info(request)


# ------------------------------------------------------------------ sign-in

def start_sign_in(request, owner: str, *, mail: bool, calendar: bool, drive: bool,
                  login_hint: str = "") -> str:
    c = client()
    if c is None:
        raise AccountError("no_client", "Sign in with Google is not set up on this OpenWings yet. Ask your admin.")
    features = {"mail": bool(mail), "calendar": bool(calendar), "drive": bool(drive)}
    if not any(features.values()):
        raise AccountError("nothing_ticked", "Tick at least one: Mail, Calendar or Drive.")
    scopes = list(BASE_SCOPES) + [FEATURE_SCOPES[k] for k, on in features.items() if on]
    flow = A.start_flow(owner, PROVIDER, "connect", scopes, redirect_uri=redirect_uri_for(request),
                        origin=A.origin_for(request), extra={"features": features})
    extra = {}
    hint = (login_hint or "").strip()
    if hint and "@" in hint and len(hint) < 255:
        extra["login_hint"] = hint
    return A.authorize_url(flow, c, extra=extra)


def email_account_authorize_url(request, owner: str, account_id: str) -> str:
    """Upstream's "Connect with Google" for an existing mail account."""
    c = client()
    if c is None:
        from fastapi import HTTPException
        raise HTTPException(400, "Sign in with Google is not set up yet: an admin adds the Google client "
                                 "in Settings > Integrations > Connect your accounts (or GOOGLE_OAUTH_CLIENT_ID in .env).")
    flow = A.start_flow(owner, PROVIDER, "email_account", (*BASE_SCOPES, SCOPE_MAIL),
                        redirect_uri=redirect_uri_for(request), origin=A.origin_for(request),
                        extra={"account_id": account_id})
    return A.authorize_url(flow, c)


def userinfo(access_token: str) -> Dict[str, str]:
    try:
        resp = A.get_json(USERINFO_URL, access_token)
        if getattr(resp, "status_code", 0) == 200:
            data = resp.json()
            if isinstance(data, dict):
                return {"email": str(data.get("email") or ""), "name": str(data.get("name") or ""),
                        "verified": "1" if data.get("email_verified", True) else ""}
    except Exception as exc:
        logger.warning("Google userinfo failed: %s", type(exc).__name__)
    return {"email": "", "name": "", "verified": ""}


def _redirect(params: Dict[str, str]):
    from fastapi.responses import RedirectResponse
    return RedirectResponse("/?" + urlencode(params), status_code=303)


def _email_back(error: Optional[str] = None):
    from fastapi.responses import RedirectResponse
    if error:
        return RedirectResponse(f"/?section=integrations&email_oauth_error={error}", status_code=303)
    return RedirectResponse("/?section=integrations&email_oauth_success=1", status_code=303)


async def callback(request, code: Optional[str], state: Optional[str], error: Optional[str]):
    """Every Google sign-in comes back here (upstream's callback path)."""
    flow = A.peek_flow(state or "")
    back = A.bounce(request, flow)
    if back is not None:
        return back
    purpose = flow.purpose if flow is not None else "email_account"
    fail = _email_back if purpose == "email_account" else (lambda e: _redirect({"ow_accounts_error": e}))
    if error:
        if flow is not None:
            try:
                A.take_flow(state or "", getattr(request.state, "current_user", None))
            except AccountError:
                pass
        return fail("google_error" if purpose == "email_account" else "denied")
    if not code or not state:
        return fail("missing_code")
    if flow is None:
        return fail("invalid_state" if purpose == "email_account" else "expired")
    from wings.email_gate import is_human_request
    if not is_human_request(request) and not A.auth_disabled():
        return fail("not_signed_in")
    user = getattr(getattr(request, "state", None), "current_user", "") or ""
    try:
        flow = A.take_flow(state, user)
    except AccountError as exc:
        return fail("invalid_state" if purpose == "email_account" else exc.code)
    c = client()
    if c is None:
        return fail("no_client")
    from starlette.concurrency import run_in_threadpool
    try:
        tokens = await run_in_threadpool(A.exchange_code, flow, code, c)
    except AccountError as exc:
        return fail("token_exchange_failed" if purpose == "email_account" else exc.code)
    if flow.purpose == "email_account" and not tokens.get("refresh_token"):
        # Upstream's rule: a reconnect must bring a fresh refresh token.
        logger.warning("Google token exchange omitted required offline credentials")
        return fail("token_exchange_failed")
    info = await run_in_threadpool(userinfo, tokens["access_token"])
    granted = A.granted_scopes(tokens, flow.scopes)
    if flow.purpose == "email_account":
        return _finish_email_account(flow, tokens, info)
    try:
        note = await finish_connect(request, flow, tokens, info, granted)
    except AccountError as exc:
        return _redirect({"ow_accounts_error": exc.code})
    params = {"ow_accounts": "google"}
    if note:
        params["ow_accounts_note"] = note
    return _redirect(params)


def _finish_email_account(flow: A.Flow, tokens: Dict[str, Any], info: Dict[str, str]):
    """Upstream's callback logic for one mail account, unchanged in what it
    checks: owner, a fresh refresh token, and the same mailbox identity."""
    from core.database import EmailAccount, SessionLocal
    from src.secret_storage import encrypt as _enc
    account_id = flow.extra.get("account_id", "")
    refresh_token = tokens.get("refresh_token") or ""
    if not refresh_token:
        logger.warning("Google token exchange omitted required offline credentials")
        return _email_back("token_exchange_failed")
    email_addr = info.get("email", "")
    db = SessionLocal()
    try:
        row = db.query(EmailAccount).filter(EmailAccount.id == account_id).first()
        if not row:
            return _email_back("account_not_found")
        if row.owner and row.owner != flow.owner:
            logger.warning("OAuth callback owner mismatch — rejecting token write")
            return _email_back("ownership_error")
        verified = email_addr.strip().casefold()
        logins = {v.strip().casefold() for v in (row.imap_user or "", row.smtp_user or "") if v.strip()}
        if not verified or any(login != verified for login in logins):
            logger.warning("Google OAuth mailbox identity verification failed for account %s", account_id)
            return _email_back("identity_verification_failed")
        _write_oauth(row, tokens, email_addr, info.get("name", ""), _enc)
        db.commit()
    finally:
        db.close()
    return _email_back(None)


def _write_oauth(row, tokens: Dict[str, Any], email_addr: str, name: str, enc) -> None:
    row.oauth_provider = "google"
    row.oauth_access_token = enc(tokens["access_token"])
    if tokens.get("refresh_token"):
        row.oauth_refresh_token = enc(tokens["refresh_token"])
    row.oauth_token_expiry = str(int(time.time()) + int(tokens.get("expires_in") or 3600))
    # Google tokens are used only with Google's own mail servers, whatever
    # the row held before.
    ihost, iport, isec = A.mail_target(*GMAIL_IMAP)
    shost, sport, ssec = A.mail_target(*GMAIL_SMTP)
    row.imap_host, row.imap_port, row.imap_starttls = ihost, iport, isec == "starttls"
    row.smtp_host, row.smtp_port, row.smtp_security = shost, sport, ssec
    if email_addr:
        row.imap_user = row.imap_user or email_addr
        row.smtp_user = row.smtp_user or email_addr
        row.from_address = row.from_address or email_addr
        if not row.name or row.name == row.id:
            row.name = email_addr
    if name and not row.display_name:
        row.display_name = name


def google_mail_account_id(owner: str, email_addr: str) -> Optional[str]:
    """This person's mail account that is this same Gmail mailbox: both
    logins are the Google address and both servers are Google's. Any other
    account (a relay, another provider, other logins) is left alone."""
    from core.database import EmailAccount, SessionLocal
    want = (email_addr or "").strip().casefold()
    if not want:
        return None
    hosts = A.mail_hosts(PROVIDER)
    db = SessionLocal()
    try:
        for row in db.query(EmailAccount).filter(EmailAccount.owner == owner).order_by(EmailAccount.created_at).all():
            logins = {(row.imap_user or "").strip().casefold(), (row.smtp_user or "").strip().casefold()}
            if logins != {want}:
                continue
            if (row.imap_host or "").strip().lower() in hosts and (row.smtp_host or "").strip().lower() in hosts:
                return row.id
        return None
    finally:
        db.close()


async def _save_mail_account(request, owner: str, email_addr: str, name: str, tokens: Dict[str, Any]) -> str:
    """A Gmail account signed in with XOAUTH2: created with upstream's own
    account route (owner stamp, default rule), then given the tokens the way
    upstream's callback stores them (encrypted). An existing account is
    reused only when it is this same Gmail mailbox on Google's servers."""
    from core.database import EmailAccount, SessionLocal
    from src.secret_storage import encrypt as _enc
    from wings.accounts_mail import route_endpoint
    account_id = google_mail_account_id(owner, email_addr)
    if account_id is None:
        create = route_endpoint(request, "/api/email/accounts", "POST")
        if create is None:
            raise AccountError("save_failed", "The mail account could not be saved.")
        ihost, iport, isec = A.mail_target(*GMAIL_IMAP)
        shost, sport, ssec = A.mail_target(*GMAIL_SMTP)
        result = await create(data={
            "name": email_addr, "imap_host": ihost, "imap_port": iport, "imap_user": email_addr,
            "imap_starttls": isec == "starttls", "smtp_host": shost, "smtp_port": sport,
            "smtp_security": ssec, "smtp_user": email_addr, "from_address": email_addr,
            "display_name": name,
        }, owner=owner, request=request)
        if not (isinstance(result, dict) and result.get("ok")):
            raise AccountError("save_failed", "The mail account could not be saved.")
        account_id = result["id"]
    db = SessionLocal()
    try:
        row = db.get(EmailAccount, account_id)
        if row is None or (row.owner and row.owner != owner):
            raise AccountError("save_failed", "The mail account could not be saved.")
        _write_oauth(row, tokens, email_addr, name, _enc)
        # A password no longer signs this account in.
        row.imap_password = _enc("")
        row.smtp_password = _enc("")
        row.enabled = True
        db.commit()
    finally:
        db.close()
    return account_id


def caldav_url(email_addr: str) -> str:
    """Google's CalDAV calendar collection for the primary calendar (its id
    is the address). Google also serves the principal at .../user, but
    upstream's sync finds calendars more reliably from the collection
    (src/caldav_sync.py, issue #2507)."""
    return A.test_url(CALDAV_BASE + quote(email_addr, safe="@") + "/events")


async def finish_connect(request, flow: A.Flow, tokens: Dict[str, Any], info: Dict[str, str],
                         granted: List[str]) -> str:
    email_addr = (info.get("email") or "").strip()
    if not email_addr:
        raise AccountError("no_email", "Google did not tell OpenWings which account signed in.")
    wanted = dict(flow.extra.get("features") or {})
    have = {k: (FEATURE_SCOPES[k] in granted) for k in FEATURE_SCOPES}
    # A later sign-in adds to what is connected: a part connected before
    # stays connected (Disconnect removes it), so signing in again with only
    # Drive ticked does not hide the mail and calendar that keep syncing.
    before = A.find_link(flow.owner, PROVIDER, account=email_addr)
    old = (before or {}).get("features") or {}
    features = {k: bool((wanted.get(k) and have[k]) or old.get(k)) for k in FEATURE_SCOPES}
    link_id = A.save_link(flow.owner, PROVIDER, email_addr, tokens, scopes=granted,
                          features=features, display_name=info.get("name", ""))
    missing = [k for k in FEATURE_SCOPES if wanted.get(k) and not have[k]]
    if wanted.get("mail") and have["mail"]:
        account_id = await _save_mail_account(request, flow.owner, email_addr, info.get("name", ""), tokens)
        A.update_features(flow.owner, link_id, email_account_id=account_id)
    if wanted.get("calendar") and have["calendar"]:
        from wings.accounts_mail import start_sync
        caldav_id = add_google_caldav(flow.owner, email_addr, link_id)
        A.update_features(flow.owner, link_id, caldav_account_id=caldav_id)
        start_sync(flow.owner)
    return ("missing_" + "_".join(missing)) if missing else ""


def add_google_caldav(owner: str, email_addr: str, link_id: str) -> str:
    """A CalDAV account for upstream's sync, signed in with this link's token
    (the URL is Google's fixed endpoint, not typed by anyone)."""
    from routes.prefs_routes import _load_for_user, _save_for_user
    from src.caldav_sync import _load_caldav_accounts
    url = caldav_url(email_addr)
    accounts = _load_caldav_accounts(owner)
    acc = next((a for a in accounts if (a.get("wings_oauth") or {}).get("link") == link_id), None)
    if acc is None:
        acc = {"id": str(uuid.uuid4())}
        accounts.append(acc)
    acc.update({"label": f"Google Calendar ({email_addr})", "url": url, "username": email_addr,
                "password": "", "wings_oauth": {"provider": PROVIDER, "link": link_id}})
    prefs = _load_for_user(owner) or {}
    prefs["caldav_accounts"] = accounts
    prefs.pop("caldav", None)
    _save_for_user(owner, prefs)
    return acc["id"]


def caldav_bearer(owner: str, acc: Dict[str, Any]) -> Tuple[str, str]:
    """(URL, bearer token) for a CalDAV account signed in with Google."""
    oauth = acc.get("wings_oauth") or {}
    if oauth.get("provider") != PROVIDER or not oauth.get("link"):
        raise AccountError("not_oauth", "Not a Google sign-in.")
    token = A.access_token(oauth["link"], owner)
    url = caldav_url(acc.get("username") or "")
    return url, token


async def disconnect(request, owner: str, link_id: str) -> bool:
    link = A.get_link(owner, link_id)
    if link is None or link["provider"] != PROVIDER:
        return False
    feats = link.get("features") or {}
    from wings.accounts_mail import remove_caldav_account, route_endpoint
    if feats.get("email_account_id"):
        delete = route_endpoint(request, "/api/email/accounts/{account_id}", "DELETE")
        if delete is not None:
            try:
                await delete(account_id=feats["email_account_id"], owner=owner)
            except Exception as exc:
                logger.info("Google mail account already gone: %s", type(exc).__name__)
    from src.caldav_sync import _load_caldav_accounts
    for acc in _load_caldav_accounts(owner):
        if (acc.get("wings_oauth") or {}).get("link") == link_id:
            remove_caldav_account(owner, acc.get("id"))
    from starlette.concurrency import run_in_threadpool
    return await run_in_threadpool(A.remove_link, owner, link_id)


def status_for(request, owner: str) -> Dict[str, Any]:
    c = client()
    links = A.links_for(owner, PROVIDER)
    return {
        "configured": c is not None,
        "source": c.source if c is not None else "",
        "links": [{"id": l["id"], "account": l["account"], "needs_signin": l["needs_signin"],
                   "mail": SCOPE_MAIL in l["scopes"] and bool(l["features"].get("mail")),
                   "calendar": SCOPE_CALENDAR in l["scopes"] and bool(l["features"].get("calendar")),
                   "drive": SCOPE_DRIVE in l["scopes"]} for l in links],
    }


# ------------------------------------------------------------------ mail token for upstream

def mail_token(account_id: str, cfg: Dict[str, Any]) -> Optional[str]:
    from routes.email.email_helpers import _get_valid_google_token
    return _get_valid_google_token(account_id, cfg)


A.MAIL_TOKEN_PROVIDERS[PROVIDER] = mail_token


# ------------------------------------------------------------------ Google Meet

def google_calendar_id(cal) -> Optional[str]:
    """The Google calendar id behind a CalDAV calendar row, from its URL
    (https://apidata.googleusercontent.com/caldav/v2/<id>/events)."""
    url = getattr(cal, "caldav_base_url", None) or ""
    if not url:
        return None
    path = urlsplit(url).path
    marker = "/caldav/v2/"
    if marker not in path:
        return None
    rest = path.split(marker, 1)[1].strip("/").split("/")
    return unquote(rest[0]) if rest and rest[0] else None


def _google_link_for_calendar(owner: str, cal) -> Optional[str]:
    from src.caldav_sync import _load_caldav_accounts
    for acc in _load_caldav_accounts(owner):
        oauth = acc.get("wings_oauth") or {}
        if oauth.get("provider") == PROVIDER and acc.get("id") == getattr(cal, "account_id", None):
            return oauth.get("link")
    return None


def is_google_calendar(owner: str, cal) -> bool:
    return bool(cal is not None and cal.source == "caldav" and _google_link_for_calendar(owner, cal)
                and google_calendar_id(cal))


def _meet_link(event: Dict[str, Any]) -> str:
    link = event.get("hangoutLink") or ""
    if not link:
        for ep in ((event.get("conferenceData") or {}).get("entryPoints") or []):
            if ep.get("entryPointType") == "video" and ep.get("uri"):
                link = ep["uri"]
                break
    return link if isinstance(link, str) and link.startswith("https://") else ""


def add_meet(owner: str, uid: str, *, http=None, sleep=time.sleep) -> Dict[str, Any]:
    """Give a Google Calendar event a Meet link; saves the link on the event."""
    from core.database import CalendarCal, CalendarEvent, SessionLocal
    db = SessionLocal()
    try:
        ev = (db.query(CalendarEvent).join(CalendarCal)
              .filter(CalendarEvent.uid == uid, CalendarCal.owner == owner).first())
        if ev is None:
            raise AccountError("not_found", "That event was not found.")
        cal = ev.calendar
        link_id = _google_link_for_calendar(owner, cal)
        cal_id = google_calendar_id(cal)
        if cal.source != "caldav" or not link_id or not cal_id:
            raise AccountError("not_google", "Google Meet links can only be added to events in a Google calendar.")
        if not ev.remote_href:
            raise AccountError("not_synced", "The event is not in Google Calendar yet, so no Meet link was added. Try again in a moment.")
    finally:
        db.close()
    token = A.access_token(link_id, owner)
    httpx = http or A._http()
    base = A.test_url(CALENDAR_API) + "/calendars/" + quote(cal_id, safe="")
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    resp = httpx.get(base + "/events", params={"iCalUID": uid, "maxResults": 5}, headers=headers, timeout=20)
    if resp.status_code != 200:
        raise AccountError("calendar_api", f"Google Calendar did not answer ({resp.status_code}). Is the Google Calendar API turned on for this install?")
    items = (resp.json() or {}).get("items") or []
    if not items:
        raise AccountError("not_synced", "Google Calendar does not have this event yet. Try again in a moment.")
    event_id = items[0].get("id")
    link = _meet_link(items[0])
    if not link:
        body = {"conferenceData": {"createRequest": {"requestId": uuid.uuid4().hex,
                                                      "conferenceSolutionKey": {"type": "hangoutsMeet"}}}}
        resp = httpx.patch(base + "/events/" + quote(str(event_id), safe=""), params={"conferenceDataVersion": 1},
                           json=body, headers=headers, timeout=20)
        if resp.status_code != 200:
            raise AccountError("meet_failed", "Google did not add a Meet link to this event.")
        event = resp.json() or {}
        link = _meet_link(event)
        for _ in range(5):
            if link:
                break
            status = (((event.get("conferenceData") or {}).get("createRequest") or {}).get("status") or {}).get("statusCode")
            if status == "failure":
                break
            sleep(1)
            r = httpx.get(base + "/events/" + quote(str(event_id), safe=""), params={"conferenceDataVersion": 1},
                          headers=headers, timeout=20)
            event = r.json() if r.status_code == 200 else {}
            link = _meet_link(event)
    if not link:
        raise AccountError("meet_failed", "Google did not add a Meet link to this event.")
    db = SessionLocal()
    try:
        ev = db.query(CalendarEvent).filter(CalendarEvent.uid == uid).first()
        if ev is not None:
            if not (ev.location or "").strip():
                ev.location = link
            if link not in (ev.description or ""):
                ev.description = ((ev.description or "").rstrip() + "\n\nGoogle Meet: " + link).strip()
            db.commit()
    finally:
        db.close()
    return {"ok": True, "meet_link": link}


# ------------------------------------------------------------------ Drive tools

def _drive_link(owner: str, account: str = "") -> Dict[str, Any]:
    for link in A.links_for(owner, PROVIDER):
        if account and link["account"] != account.strip().lower():
            continue
        if SCOPE_DRIVE in link["scopes"]:
            return link
    raise AccountError("drive_not_connected", "Google Drive is not connected for you. Connect it in Settings > "
                       "Integrations > Connect your accounts > Google Calendar and Drive.")


def has_drive(owner: str) -> bool:
    """Whether this person connected Google Drive (the Drive tools show in
    their chats only then)."""
    return any(SCOPE_DRIVE in l["scopes"] and not l["needs_signin"] for l in A.links_for(owner, PROVIDER))


def _q_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("'", "\\'")


def drive_search(owner: str, query: str, *, max_results: int = 10, account: str = "", http=None) -> Dict[str, Any]:
    query = (query or "").strip()
    if not query:
        raise AccountError("no_query", "Say what to look for.")
    if len(query) > 200:
        query = query[:200]
    link = _drive_link(owner, account)
    token = A.access_token(link["id"], owner)
    n = max(1, min(int(max_results or 10), 25))
    q = f"(name contains '{_q_escape(query)}' or fullText contains '{_q_escape(query)}') and trashed = false"
    resp = (http or A._http()).get(A.test_url(DRIVE_API) + "/files", params={
        "q": q, "pageSize": n, "spaces": "drive",
        "fields": "files(id,name,mimeType,modifiedTime,size,webViewLink)",
        "includeItemsFromAllDrives": "true", "supportsAllDrives": "true", "corpora": "allDrives",
    }, headers={"Authorization": f"Bearer {token}", "Accept": "application/json"}, timeout=20)
    if resp.status_code == 403:
        raise AccountError("drive_api", "Google Drive refused the search. Is the Drive API turned on for this install?")
    if resp.status_code != 200:
        raise AccountError("drive_api", f"Google Drive did not answer ({resp.status_code}).")
    files = (resp.json() or {}).get("files") or []
    return {"account": link["account"], "files": [
        {"id": f.get("id"), "name": f.get("name"), "type": f.get("mimeType"), "modified": f.get("modifiedTime"),
         "size": f.get("size"), "link": f.get("webViewLink")} for f in files if isinstance(f, dict)]}


EXPORTS = {
    "application/vnd.google-apps.document": ("text/plain", ".txt"),
    "application/vnd.google-apps.spreadsheet": ("text/csv", ".csv"),
    "application/vnd.google-apps.presentation": ("text/plain", ".txt"),
}
DOWNLOADS = {
    "application/pdf": ".pdf",
    "text/plain": ".txt", "text/markdown": ".md", "text/csv": ".csv", "text/tab-separated-values": ".tsv",
    "application/json": ".json", "text/xml": ".xml", "application/xml": ".xml",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
}


def _read_capped(resp) -> bytes:
    data = b""
    for chunk in resp.iter_bytes():
        data += chunk
        if len(data) > MAX_FILE_BYTES:
            raise AccountError("too_big", "That file is larger than 10 MB, so it was not read.")
    return data


def drive_read(owner: str, file_id: str, *, max_chars: int = 12000, account: str = "", http=None) -> Dict[str, Any]:
    file_id = (file_id or "").strip()
    if not FILE_ID_RE.match(file_id):
        raise AccountError("bad_id", "Use a file id from google_drive_search.")
    max_chars = max(500, min(int(max_chars or 12000), 50000))
    link = _drive_link(owner, account)
    token = A.access_token(link["id"], owner)
    httpx = http or A._http()
    headers = {"Authorization": f"Bearer {token}"}
    base = A.test_url(DRIVE_API) + "/files/" + quote(file_id, safe="")
    meta = httpx.get(base, params={"fields": "id,name,mimeType,size", "supportsAllDrives": "true"},
                     headers=headers, timeout=20)
    if meta.status_code == 404:
        raise AccountError("not_found", "No such file in this person's Google Drive.")
    if meta.status_code != 200:
        raise AccountError("drive_api", f"Google Drive did not answer ({meta.status_code}).")
    info = meta.json() or {}
    mime = str(info.get("mimeType") or "")
    name = str(info.get("name") or file_id)
    if mime in EXPORTS:
        export_mime, suffix = EXPORTS[mime]
        url, params = base + "/export", {"mimeType": export_mime}
    elif mime in DOWNLOADS:
        try:
            if int(info.get("size") or 0) > MAX_FILE_BYTES:
                raise AccountError("too_big", "That file is larger than 10 MB, so it was not read.")
        except ValueError:
            pass
        suffix = DOWNLOADS[mime]
        url, params = base, {"alt": "media", "supportsAllDrives": "true"}
    else:
        return {"name": name, "type": mime, "content_status": "unsupported",
                "content_note": "OpenWings can read Google Docs, Sheets, Slides, PDFs and text files from Drive."}
    with httpx.stream("GET", url, params=params, headers=headers, timeout=30) as resp:
        if resp.status_code == 403:
            raise AccountError("too_big", "Google would not export that file (exports stop at 10 MB).")
        if resp.status_code != 200:
            raise AccountError("drive_api", f"Google Drive did not hand over the file ({resp.status_code}).")
        data = _read_capped(resp)
    from src.email_attachment_text import attachment_text
    with tempfile.TemporaryDirectory(prefix="ow-drive-") as tmp:
        path = os.path.join(tmp, "file" + suffix)
        with open(path, "wb") as fh:
            fh.write(data)
        result = attachment_text(path, max_chars=max_chars)
    return {"name": name, "type": mime, **result}


async def _tool_search(args: Dict[str, Any], owner: str) -> Dict[str, Any]:
    out = await asyncio.to_thread(drive_search, owner, str(args.get("query") or ""),
                                  max_results=args.get("max_results") or 10, account=str(args.get("account") or ""))
    if not out["files"]:
        return {"response": f"No files in Google Drive ({out['account']}) match that.", "files": [], "exit_code": 0}
    lines = [f"Google Drive ({out['account']}), {len(out['files'])} file(s):"]
    for f in out["files"]:
        lines.append(f"- {f['name']} | id: {f['id']} | {f['type']} | modified {f.get('modified') or '?'}")
    return {"response": "\n".join(lines), "files": out["files"], "exit_code": 0}


async def _tool_read(args: Dict[str, Any], owner: str) -> Dict[str, Any]:
    out = await asyncio.to_thread(drive_read, owner, str(args.get("file_id") or args.get("id") or ""),
                                  max_chars=args.get("max_chars") or 12000, account=str(args.get("account") or ""))
    if out.get("content"):
        note = f"\n[{out['content_note']}]" if out.get("content_note") else ""
        return {"response": f"{out['name']} ({out['type']}):\n{out['content']}{note}", "exit_code": 0}
    return {"response": f"{out['name']}: {out.get('content_note') or out.get('content_status')}", "exit_code": 0}


DRIVE_WORDS = (r"\b(?:google\s+drive|my\s+drive|in\s+drive|on\s+drive|drive\s+(?:files?|folders?|docs?)|"
               r"google\s+docs?|google\s+sheets?|google\s+slides|gdrive)\b")

A.register_tool(AccountTool(
    name="google_drive_search", family="cloud_files", family_label="Your Google Drive (read only)",
    words=DRIVE_WORDS,
    description=("Search the signed-in person's own Google Drive (read only) by file name and contents. "
                 "Returns file names and ids; then read one with google_drive_read. Works only after the "
                 "person connected Google Drive in Settings > Integrations > Connect your accounts."),
    parameters={"type": "object", "properties": {
        "query": {"type": "string", "description": "Words to look for in file names and contents."},
        "max_results": {"type": "integer", "minimum": 1, "maximum": 25, "description": "How many files (default 10)."},
        "account": {"type": "string", "description": "Google address, only when the person connected more than one."},
    }, "required": ["query"], "additionalProperties": False},
    handler=_tool_search, available=has_drive,
))
A.register_tool(AccountTool(
    name="google_drive_read", family="cloud_files", family_label="Your Google Drive (read only)",
    words=DRIVE_WORDS,
    description=("Read one file from the signed-in person's Google Drive by id (from google_drive_search). "
                 "Google Docs and Slides come back as text, Sheets as CSV; PDFs and text files are read too. "
                 "Up to 10 MB; long files are cut."),
    parameters={"type": "object", "properties": {
        "file_id": {"type": "string", "description": "The id from google_drive_search."},
        "max_chars": {"type": "integer", "minimum": 500, "maximum": 50000, "description": "Most characters to return (default 12000)."},
        "account": {"type": "string", "description": "Google address, only when the person connected more than one."},
    }, "required": ["file_id"], "additionalProperties": False},
    handler=_tool_read, available=has_drive,
))
