"""Connect your accounts (patch 0025): the routes behind the cards in
Settings > Integrations.

  GET    /api/wings/accounts/status                 this person's cards
  POST   /api/wings/accounts/mail/connect           Gmail / iCloud / Yahoo with an app password
  DELETE /api/wings/accounts/mail/{account_id}      remove that mail account (and iCloud calendars)
  POST   /api/wings/accounts/calendar-link          subscribe to a calendar by link
  POST   /api/wings/accounts/calendar-link/{id}/refresh
  DELETE /api/wings/accounts/calendar-link/{id}
  GET    /api/wings/accounts/google/setup           admin: the own key wizard
  POST   /api/wings/accounts/google/setup           admin: save Client ID and secret
  POST   /api/wings/accounts/google/setup/redirect  admin: use the address OpenWings is opened at now
  DELETE /api/wings/accounts/google/setup           admin: remove the saved client
  POST   /api/wings/accounts/google/start           Sign in with Google (Mail, Calendar, Drive)
  DELETE /api/wings/accounts/google/{link_id}       sign out of that Google account
  POST   /api/wings/accounts/google/meet            add a Meet link to a Google event
  GET    /api/email/oauth/google/callback           (upstream's path, patched to wings.accounts_google)

Every route acts for the signed-in person in their own browser
(wings/email_gate.is_human_request): not the assistant, not an API token.
The install-wide Google client is admin only. Secrets never go back to the
browser; the client id is shown masked.

Requests that change something must come from OpenWings' own pages: a body
must be JSON (Content-Type application/json, which a plain form on another
site cannot send without the browser asking first), and a request the
browser marks as coming from another site (Sec-Fetch-Site), or with an
Origin this install does not trust, is refused.
"""
from __future__ import annotations

import logging
from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from wings import accounts as A
from wings.accounts import AccountError

logger = logging.getLogger(__name__)


def _err(exc: AccountError, status: int = 400) -> JSONResponse:
    return JSONResponse({"ok": False, "error": exc.code, "message": exc.message}, status_code=status)


def _is_json(content_type: str) -> bool:
    media = (content_type or "").split(";", 1)[0].strip().lower()
    return media == "application/json" or (media.startswith("application/") and media.endswith("+json"))


async def _body(request: Request) -> Dict[str, Any]:
    if not _is_json(request.headers.get("content-type", "")):
        raise HTTPException(415, "Send JSON (Content-Type: application/json).")
    try:
        data = await request.json()
    except Exception:
        data = {}
    return data if isinstance(data, dict) else {}


def same_origin_only(request: Request) -> None:
    """Refuse a change that another site made the browser send (login CSRF
    and its relatives): Sec-Fetch-Site other than same-origin or none, or an
    Origin this install does not trust. Reads (GET) are not affected."""
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return
    site = (request.headers.get("sec-fetch-site") or "").strip().lower()
    if site and site not in ("same-origin", "none"):
        raise HTTPException(403, "This request did not come from OpenWings' own page.")
    origin = (request.headers.get("origin") or "").strip()
    if origin:
        from wings.origin import _norm, allowed_origins, request_origin
        n = _norm(origin)
        if not n or (n not in allowed_origins() and n != request_origin(request)):
            raise HTTPException(403, "This request did not come from OpenWings' own page.")


def app_states(mcp_manager) -> Dict[str, bool]:
    """slug -> whether the install-wide tool server of that work app is
    connected right now (no secrets, a yes or no for members' cards)."""
    from wings.connect_routes import APPS
    out = {slug: False for slug in APPS}
    if mcp_manager is None:
        return out
    try:
        from core.database import McpServer, SessionLocal
        db = SessionLocal()
        try:
            rows = db.query(McpServer).filter(McpServer.is_enabled == True).all()  # noqa: E712
            servers = [(r.id, (r.url or "").rstrip("/")) for r in rows]
        finally:
            db.close()
        for slug, app in APPS.items():
            url = app["url"].rstrip("/")
            for sid, surl in servers:
                if surl == url and (mcp_manager.get_server_status(sid) or {}).get("status") == "connected":
                    out[slug] = True
                    break
    except Exception as exc:
        logger.info("Work app states unavailable: %s", type(exc).__name__)
    return out


def on_startup() -> None:
    """Called from upstream's app start-up (app.py): assistant tools and the
    calendar link refresh loop."""
    A.warn_test_hooks()
    A.load_providers()
    A.install_tools()
    from wings import ics_feeds
    ics_feeds.start_background()


def setup_accounts_routes(mcp_manager=None) -> APIRouter:
    A.load_providers()
    try:
        A.install_tools()
    except Exception:
        logger.exception("Assistant account tools not added")
    router = APIRouter(prefix="/api/wings/accounts", tags=["wings-accounts"],
                       dependencies=[Depends(same_origin_only)])

    from wings.connect_routes import APPS

    @router.get("/status")
    def status(request: Request):
        user = A.person(request)
        from wings import accounts_google, accounts_mail, ics_feeds
        admin = A.person_is_admin(request)
        try:
            mail = accounts_mail.mail_accounts(user)
        except Exception:
            mail = []
        try:
            caldav = accounts_mail.caldav_accounts(user)
        except Exception:
            caldav = []
        states = app_states(mcp_manager)
        return {
            "admin": admin,
            "mail_providers": accounts_mail.provider_cards(),
            "mail": mail,
            "caldav": caldav,
            "feeds": ics_feeds.feeds_for(user),
            "feed_help": ics_feeds.PROVIDER_HELP,
            "google": accounts_google.status_for(request, user),
            # The one-click work apps of patch 0021 (an admin connects them
            # for everyone); members see them too, without buttons.
            "apps": [{"slug": slug, "name": a["name"], "url": a["url"], "about": a["about"], "note": a["note"],
                      "connected": states.get(slug, False)}
                     for slug, a in APPS.items()],
        }

    # -------------------------------------------------------------- app passwords

    @router.post("/mail/connect")
    async def mail_connect(request: Request):
        user = A.person(request)
        body = await _body(request)
        from wings import accounts_mail
        try:
            return await accounts_mail.connect(
                request, user, str(body.get("provider") or ""), str(body.get("address") or ""),
                str(body.get("password") or ""), calendar=bool(body.get("calendar")))
        except AccountError as exc:
            return _err(exc)

    @router.delete("/mail/{account_id}")
    async def mail_remove(account_id: str, request: Request):
        user = A.person(request)
        from wings import accounts_mail
        from wings.accounts_mail import route_endpoint
        accounts = {a["id"]: a for a in accounts_mail.mail_accounts(user)}
        acct = accounts.get(account_id)
        if acct is None:
            raise HTTPException(404, "Account not found")
        delete = route_endpoint(request, "/api/email/accounts/{account_id}", "DELETE")
        if delete is None:
            raise HTTPException(503, "Mail is not available")
        await delete(account_id=account_id, owner=user)
        removed_calendar = False
        if acct["provider"] == "icloud":
            for cal in accounts_mail.caldav_accounts(user):
                if cal["kind"] == "icloud" and cal["username"].lower() == (acct["address"] or "").lower():
                    removed_calendar = accounts_mail.remove_caldav_account(user, cal["id"]) or removed_calendar
        return {"ok": True, "calendar_removed": removed_calendar}

    # -------------------------------------------------------------- calendar by link

    @router.post("/calendar-link")
    async def calendar_link_add(request: Request):
        user = A.person(request)
        body = await _body(request)
        from starlette.concurrency import run_in_threadpool
        from wings import ics_feeds
        try:
            return await run_in_threadpool(ics_feeds.subscribe, user, str(body.get("url") or ""),
                                           name=str(body.get("name") or ""), color=str(body.get("color") or ""))
        except AccountError as exc:
            return _err(exc)

    @router.post("/calendar-link/{feed_id}/refresh")
    async def calendar_link_refresh(feed_id: str, request: Request):
        user = A.person(request)
        from starlette.concurrency import run_in_threadpool
        from wings import ics_feeds
        try:
            return await run_in_threadpool(ics_feeds.refresh_feed, user, feed_id)
        except AccountError as exc:
            return _err(exc, 404 if exc.code == "not_found" and "subscribed" in exc.message else 400)

    @router.delete("/calendar-link/{feed_id}")
    def calendar_link_remove(feed_id: str, request: Request):
        user = A.person(request)
        from wings import ics_feeds
        if not ics_feeds.unsubscribe(user, feed_id):
            raise HTTPException(404, "Not subscribed")
        return {"ok": True}

    # -------------------------------------------------------------- Google own key

    @router.get("/google/setup")
    def google_setup(request: Request):
        A.require_admin_person(request)
        from wings import accounts_google
        return accounts_google.setup_info(request)

    @router.post("/google/setup")
    async def google_setup_save(request: Request):
        user = A.require_admin_person(request)
        body = await _body(request)
        from wings import accounts_google
        try:
            return {"ok": True, **accounts_google.save_setup(
                request, user, str(body.get("client_id") or ""), str(body.get("client_secret") or ""))}
        except AccountError as exc:
            return _err(exc)

    @router.post("/google/setup/redirect")
    async def google_setup_redirect(request: Request):
        user = A.require_admin_person(request)
        await _body(request)
        from wings import accounts_google
        try:
            return {"ok": True, **accounts_google.use_current_redirect(request, user)}
        except AccountError as exc:
            return _err(exc)

    @router.delete("/google/setup")
    def google_setup_remove(request: Request):
        A.require_admin_person(request)
        from wings import accounts_google
        removed = A.delete_client(accounts_google.PROVIDER)
        return {"ok": True, "removed": removed, **accounts_google.setup_info(request)}

    @router.post("/google/start")
    async def google_start(request: Request):
        user = A.person(request)
        body = await _body(request)
        from wings import accounts_google
        try:
            url = accounts_google.start_sign_in(
                request, user, mail=bool(body.get("mail")), calendar=bool(body.get("calendar")),
                drive=bool(body.get("drive")), login_hint=str(body.get("login_hint") or ""))
        except AccountError as exc:
            return _err(exc)
        return {"ok": True, "authorize_url": url}

    @router.delete("/google/{link_id}")
    async def google_remove(link_id: str, request: Request):
        user = A.person(request)
        from wings import accounts_google
        if not await accounts_google.disconnect(request, user, link_id):
            raise HTTPException(404, "Not connected")
        return {"ok": True}

    @router.post("/google/meet")
    async def google_meet(request: Request):
        user = A.person(request)
        body = await _body(request)
        from starlette.concurrency import run_in_threadpool
        from wings import accounts_google
        try:
            return await run_in_threadpool(accounts_google.add_meet, user, str(body.get("uid") or ""))
        except AccountError as exc:
            return _err(exc)

    return router
