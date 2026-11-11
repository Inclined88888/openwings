"""Continue with ChatGPT routes (patch 0022). The logic is in wings/chatgpt_plan.py.

  POST /api/wings/connect/chatgpt/start       begin a sign-in (127.0.0.1 only)
  GET  /api/wings/connect/chatgpt/callback    OpenAI sends the browser here
  POST /api/wings/connect/chatgpt/disconnect  sign out (revoke, clear tokens)
  POST /api/wings/connect/chatgpt/forget      use a different ChatGPT account
  POST /api/wings/connect/chatgpt/models      load the model list again
  POST /api/wings/connect/chatgpt/welcomed    the first-use note was seen

Only a signed-in OpenWings admin connects ChatGPT, for themselves: the
connection, its endpoint and its tokens belong to that one person.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional
from urllib.parse import urlencode

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from starlette.concurrency import run_in_threadpool

from wings import chatgpt_plan as cp

logger = logging.getLogger(__name__)


def _person(request: Request) -> str:
    from wings.connect_routes import _person_admin
    user = _person_admin(request)
    if not user:
        raise HTTPException(403, "Sign in to OpenWings first.")
    return user


def _back(**params: str) -> RedirectResponse:
    # A fixed path of this app (relative, so the browser stays on 127.0.0.1).
    return RedirectResponse("/?" + urlencode(params), status_code=303)


def access_info(request: Request) -> Dict[str, Any]:
    """How this browser can sign in to ChatGPT (no account data)."""
    lb = cp.loopback_of(request)
    info: Dict[str, Any] = {"usage_url": cp.USAGE_URL, "server_port": cp.server_port()}
    if lb is not None and lb.host == "127.0.0.1":
        info["mode"] = "ready"
    elif lb is not None:
        info["mode"] = "switch"
        info["switch_url"] = f"http://127.0.0.1:{lb.port}/?ow_open=chatgpt"
    else:
        info["mode"] = "remote"
    return info


def status_payload(request: Request, user: str) -> Dict[str, Any]:
    out = access_info(request)
    out["user"] = user
    try:
        out.update(cp.status_for(user) if user else {"state": "none"})
    except Exception as exc:
        logger.warning("ChatGPT status unavailable: %s", type(exc).__name__)
        out["state"] = "none"
    return out


def setup_chatgpt_routes() -> APIRouter:
    router = APIRouter(tags=["wings-connect"])

    @router.post("/api/wings/connect/chatgpt/start")
    async def chatgpt_start(request: Request):
        user = _person(request)
        try:
            body = await request.json()
        except Exception:
            body = {}
        info = access_info(request)
        if info["mode"] != "ready":
            return JSONResponse({"ok": False, **info}, status_code=409)
        lb = cp.loopback_of(request)
        host = await run_in_threadpool(cp.host_id)

        def _account():
            db = cp._db()
            try:
                link = cp.get_link(db, user)
                return (link.client_id, link.email, bool(link.plan_enabled)) if link else (None, None, False)
            finally:
                db.close()

        client_id, email, plan_on = await run_in_threadpool(_account)
        consent = bool(isinstance(body, dict) and body.get("consent")) and client_id is not None and not plan_on
        flow = cp.start_sign_in(user, lb.port, client_id, consent=consent)
        return {"ok": True, "authorize_url": cp.authorize_url(flow, host, login_hint=email)}

    @router.get(cp.CALLBACK_PATH)
    async def chatgpt_callback(request: Request, code: str = "", state: str = "",
                               client_id: Optional[str] = None, error: str = ""):
        from wings.connect_routes import _is_person_admin
        if not _is_person_admin(request):
            return _back(ow_chatgpt="not_admin")
        user = getattr(request.state, "current_user", "") or ""
        try:
            flow = cp.take_sign_in(state, user)
        except cp.PlanError as exc:
            return _back(ow_chatgpt=exc.code)
        if error:
            # Declined, or OpenAI stopped it: nothing is exchanged.
            return _back(ow_chatgpt="denied" if error == "access_denied" else "error")
        if not code:
            return _back(ow_chatgpt="no_code")
        try:
            issued = cp.callback_client_id(flow, client_id)
            if flow.client_id is None:
                await run_in_threadpool(cp.remember_client_id, user, issued)
            tokens = await run_in_threadpool(cp.exchange_code, code, flow.verifier, issued, flow.redirect_uri)
            claims = await run_in_threadpool(
                lambda: cp.validate_id_token(tokens["id_token"], client_id=issued, nonce=flow.nonce)
            )
            models = None
            if cp.PLAN_SCOPE in cp.granted_scopes(tokens):
                try:
                    models = await run_in_threadpool(cp.fetch_models, tokens["access_token"])
                except cp.PlanError:
                    models = None
            result = await run_in_threadpool(
                lambda: cp.store_sign_in(user, issued, tokens, claims, models=models)
            )
        except cp.PlanError as exc:
            return _back(ow_chatgpt=exc.code)
        return _back(ow_chatgpt="connected" if result["plan_enabled"] else "plan_off")

    @router.post("/api/wings/connect/chatgpt/disconnect")
    async def chatgpt_disconnect(request: Request):
        user = _person(request)
        return await run_in_threadpool(cp.sign_out, user)

    @router.post("/api/wings/connect/chatgpt/forget")
    async def chatgpt_forget(request: Request):
        user = _person(request)
        await run_in_threadpool(cp.forget, user)
        return {"ok": True}

    @router.post("/api/wings/connect/chatgpt/models")
    async def chatgpt_models(request: Request):
        user = _person(request)

        def _reload():
            _auth_id, access = cp.access_token_for(user)
            if not access:
                raise cp.PlanReauthRequired()
            return cp.update_models(user, cp.fetch_models(access))

        try:
            n = await run_in_threadpool(_reload)
        except cp.PlanError as exc:
            return JSONResponse({"ok": False, "error": exc.code, "message": exc.message}, status_code=400)
        return {"ok": True, "models": n}

    @router.post("/api/wings/connect/chatgpt/welcomed")
    async def chatgpt_welcomed(request: Request):
        user = _person(request)
        await run_in_threadpool(cp.mark_welcomed, user)
        return {"ok": True}

    return router
