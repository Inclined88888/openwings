"""WINGS_FORCE_EMAIL_CONFIRM: no email leaves without a person's click.

Upstream's confirmation for agent-written mail is a setting the agent itself
can switch off, and several paths (scheduled task output, away auto-replies,
the agent's in-process `app_api` calls to the send route, plugin tokens) send
with no human step at all. With WINGS_FORCE_EMAIL_CONFIRM on (the OpenWings
default) the rule is enforced at the SMTP chokepoint instead:

* A send is allowed only when the caller marks it ``human_approved``. Only
  routes serving a signed-in person's browser session may do that: compose
  Send, Schedule Send, approving a staged draft, and the unsubscribe button.
* Automated sends whose every recipient is listed in WINGS_EMAIL_SELF_ADDRESSES
  (set only in .env, out of the agent's reach) are allowed, so task results
  and reminders can still be mailed to yourself.
* Everything else raises EmailSendBlocked. Agent send/reply tools turn into
  draft documents with a Send button instead.

The variable lives only in the deployment environment; nothing inside the app,
including the agent's settings tool, can turn it off.
"""
from __future__ import annotations

import email.utils
import os
from typing import Iterable

_OFF = {"0", "false", "no", "off"}
INTERNAL_TOOL_HEADER = "X-Odysseus-Internal-Token"  # mirrors core.middleware


class EmailSendBlocked(PermissionError):
    """Raised when WINGS_FORCE_EMAIL_CONFIRM refuses an outbound message."""


def force_confirm_enabled() -> bool:
    return os.environ.get("WINGS_FORCE_EMAIL_CONFIRM", "1").strip().lower() not in _OFF


def self_addresses() -> set[str]:
    raw = os.environ.get("WINGS_EMAIL_SELF_ADDRESSES", "")
    return {a.strip().lower() for a in raw.replace(";", ",").split(",") if a.strip()}


def _addr(value: str) -> str:
    return (email.utils.parseaddr(value or "")[1] or "").strip().lower()


def is_human_request(request) -> bool:
    """True only for a signed-in person's own session-cookie request.

    Not human: the agent's in-process loopback calls (they carry the internal
    tool token header and may impersonate the owner), API bearer tokens
    (plugins, scripts), auth-disabled installs, and direct function calls
    that pass no request at all.
    """
    if request is None:
        return False
    if os.environ.get("AUTH_ENABLED", "true").strip().lower() in _OFF:
        return False
    try:
        if request.headers.get(INTERNAL_TOOL_HEADER):
            return False
    except Exception:
        return False
    state = getattr(request, "state", None)
    if state is None or getattr(state, "api_token", False):
        return False
    return bool(getattr(state, "current_user", None))


def require_human(request, action: str = "Sending email") -> None:
    """Raise HTTP 403 unless a person (not the agent or a token) made this request."""
    if not force_confirm_enabled() or is_human_request(request):
        return
    from fastapi import HTTPException

    raise HTTPException(
        status_code=403,
        detail=(
            f"{action} needs a signed-in person in the OpenWings web app "
            "(WINGS_FORCE_EMAIL_CONFIRM is on). Agents and API tokens can create "
            "drafts; a person reviews and clicks Send."
        ),
    )


def check_send(recipients: Iterable[str], *, human_approved: bool) -> None:
    """Chokepoint check, called right before SMTP delivery."""
    if not force_confirm_enabled() or human_approved:
        return
    rcpts = {_addr(r) for r in recipients if _addr(r)}
    allowed = self_addresses()
    if rcpts and allowed and rcpts <= allowed:
        return
    raise EmailSendBlocked(
        "Blocked by WINGS_FORCE_EMAIL_CONFIRM: this message was not sent because "
        "no person approved it. Agents create drafts; open the draft and click Send. "
        "To let automated mail reach only yourself, list your own addresses in "
        "WINGS_EMAIL_SELF_ADDRESSES in .env."
    )


# --- Scheduled-send rows: only rows a person created or approved may go out.

SCHEDULED_FLAG_COLUMN = "wings_human_approved"


def ensure_scheduled_flag(conn, *, approve_existing: bool = False) -> None:
    """Add the approval flag column to scheduled_emails if missing (idempotent).

    approve_existing=True is used once, by the container preflight, when an
    upstream Odysseus volume is first started under OpenWings: rows that were
    already pending were created by upstream's own Schedule Send. At runtime
    the column is added with every row unapproved."""
    try:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(scheduled_emails)")}
    except Exception:
        return
    if cols and SCHEDULED_FLAG_COLUMN not in cols:
        conn.execute(
            f"ALTER TABLE scheduled_emails ADD COLUMN {SCHEDULED_FLAG_COLUMN} "
            "INTEGER NOT NULL DEFAULT 0"
        )
        if approve_existing:
            where = "status = 'pending'"
            if "odysseus_kind" in cols:
                where += " AND COALESCE(odysseus_kind, '') != 'agent_draft'"
            conn.execute(f"UPDATE scheduled_emails SET {SCHEDULED_FLAG_COLUMN} = 1 WHERE {where}")
        conn.commit()


def mark_scheduled_approved(conn, sid: str) -> None:
    ensure_scheduled_flag(conn)
    conn.execute(
        f"UPDATE scheduled_emails SET {SCHEDULED_FLAG_COLUMN} = 1 WHERE id = ?", (sid,)
    )
    conn.commit()


def scheduled_row_approved(conn, sid: str) -> bool:
    ensure_scheduled_flag(conn)
    row = conn.execute(
        f"SELECT {SCHEDULED_FLAG_COLUMN} FROM scheduled_emails WHERE id = ?", (sid,)
    ).fetchone()
    return bool(row and row[0])
