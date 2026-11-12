"""Routes for Import from Hermes (wings/hermes_import.py).

All of them act for the signed-in person only and need the
"can_manage_memory" privilege. Nothing is written by /preview; /import takes
the preview token, so the server never trusts text edited in the browser.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List

from fastapi import APIRouter, Depends, HTTPException, Request

from wings import hermes_import as H

logger = logging.getLogger("wings.hermes_routes")


# Room for the multipart envelope around a 50 MB archive.
_MAX_REQUEST = H.MAX_UPLOAD + 1024 * 1024


def setup_hermes_routes(memory_manager, session_manager=None, memory_vector=None) -> APIRouter:
    # Every change must come from OpenWings' own page: /preview is a multipart
    # POST a cross-site form could send with the session cookie.
    from wings.accounts_routes import _is_json, same_origin_only
    router = APIRouter(prefix="/api/wings/hermes", tags=["wings-hermes"],
                       dependencies=[Depends(same_origin_only)])

    def _who(request: Request) -> str:
        from src.auth_helpers import require_privilege
        if getattr(request.state, "api_token", False):
            raise HTTPException(403, "Import from Hermes is only available when signed in.")
        user = require_privilege(request, "can_manage_memory") or getattr(request.state, "current_user", "") or None
        try:
            # Installs with sign in switched off store everything for the
            # local owner; the import must land in the same place.
            from src.owner_identity import effective_storage_owner
            user = effective_storage_owner(user)
        except ImportError:
            pass
        if not user:
            raise HTTPException(401, "Sign in first.")
        return user

    def _problem(exc: Exception):
        return HTTPException(400, str(exc))

    @router.post("/preview")
    async def preview(request: Request) -> Dict[str, Any]:
        user = _who(request)
        # Refuse an oversized upload before receiving it (the form parser
        # would otherwise spool all of it to disk first).
        try:
            declared = int(request.headers.get("content-length") or 0)
        except ValueError:
            declared = 0
        if declared > _MAX_REQUEST:
            raise HTTPException(413, "That file is larger than 50 MB. Pack only the Hermes memory and "
                                     "chats with the command shown.")
        try:
            form = await request.form(max_files=1, max_fields=4, max_part_size=2 * 1024 * 1024)
        except Exception:
            raise HTTPException(400, "That upload could not be read. Upload the file the command saved, "
                                     "or paste less text.")
        file = form.get("file")
        text = form.get("text")
        text = text if isinstance(text, str) else None
        include_background = form.get("include_background")
        include_background = include_background if isinstance(include_background, str) else None
        try:
            existing = memory_manager.load(owner=user)
        except Exception:
            raise HTTPException(503, "Your memory could not be read right now. Nothing was changed.")
        try:
            if file is not None and not isinstance(file, str) and getattr(file, "filename", None):
                if (getattr(file, "size", None) or 0) > H.MAX_UPLOAD:
                    raise H.ImportProblem("That file is larger than 50 MB. Pack only the Hermes memory "
                                          "and chats with the command shown.")
                data = await file.read(H.MAX_UPLOAD + 1)
                up = await asyncio.to_thread(H.read_upload, file.filename or "", data)
                return await asyncio.to_thread(
                    H.preview, user, existing, upload=up,
                    include_background=str(include_background or "").lower() in ("1", "true", "on"))
            if text is not None:
                return await asyncio.to_thread(H.preview, user, existing, pasted=text)
        except H.ImportProblem as exc:
            raise _problem(exc)
        except (ValueError, RecursionError, OSError, EOFError):
            logger.info("Hermes preview could not read an upload", exc_info=True)
            raise HTTPException(400, "This file could not be read. Upload the file the command saved on your Desktop.")
        raise HTTPException(400, "Upload a file or paste text.")

    @router.post("/import")
    async def do_import(request: Request) -> Dict[str, Any]:
        user = _who(request)
        if not _is_json(request.headers.get("content-type", "")):
            raise HTTPException(415, "Send JSON (Content-Type: application/json).")
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(400, "Bad request.")
        token = str((body or {}).get("token") or "")
        mem: List[str] = [str(k) for k in (body or {}).get("memories") or []][:20000]
        chats: List[str] = [str(k) for k in (body or {}).get("chats") or []][:20000]
        try:
            return await asyncio.to_thread(H.apply, user, token, mem, chats,
                                           memory_manager=memory_manager, memory_vector=memory_vector)
        except H.ImportProblem as exc:
            raise _problem(exc)
        except Exception:
            logger.exception("Hermes import failed")
            raise HTTPException(500, "The import did not finish. Nothing was kept from it.")

    @router.get("/imports")
    async def imports(request: Request) -> Dict[str, Any]:
        user = _who(request)
        return {"imports": await asyncio.to_thread(H.batches, user)}

    @router.post("/undo/{batch_id}")
    async def undo(batch_id: str, request: Request) -> Dict[str, Any]:
        user = _who(request)
        try:
            out = await asyncio.to_thread(H.undo, user, batch_id,
                                          memory_manager=memory_manager, memory_vector=memory_vector)
        except H.ImportProblem as exc:
            raise HTTPException(404, str(exc))
        cache = getattr(session_manager, "sessions", None)
        if isinstance(cache, dict):
            for sid in out.get("session_ids") or []:
                cache.pop(sid, None)
        return out

    @router.get("/read-only/{session_id}")
    async def read_only(session_id: str, request: Request) -> Dict[str, Any]:
        user = _who(request)
        # Only the person's own imported chats: an id alone says nothing.
        return {"read_only": H.is_read_only(session_id, owner=user), "message": H.READ_ONLY_MESSAGE}

    return router
