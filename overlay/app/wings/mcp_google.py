"""Google MCP sign-ins (upstream's own Google flow), made safe (patch 0021).

Upstream sent the MCP server id as the OAuth `state`: a fixed, reusable value
with no PKCE and no record of who started the sign-in, so any signed-in admin
who opened the callback with someone else's code finished it (a login CSRF
that could bind another Google account to this install's Google tools).

Now each attempt gets a random state bound to the person who started it, a
ten-minute expiry and an S256 PKCE pair. The callback takes the server id from
this record, never from the state, finishes only for that same person (an
admin's own browser when the attempt had no owner), and only once.
"""
from __future__ import annotations

import secrets
import time
from dataclasses import dataclass, field
from typing import Optional

from wings.connect_routes import new_verifier, s256_challenge

FLOW_TTL_SECONDS = 600
MAX_PENDING = 64


@dataclass
class GoogleFlow:
    server_id: str
    owner: Optional[str]
    verifier: str
    redirect_uri: str
    created: float = field(default_factory=time.monotonic)


_flows: dict[str, GoogleFlow] = {}


class NotYours(Exception):
    """The attempt exists but was started by someone else."""


def _prune(now: Optional[float] = None) -> None:
    now = time.monotonic() if now is None else now
    for k in [k for k, f in _flows.items() if now - f.created > FLOW_TTL_SECONDS]:
        _flows.pop(k, None)
    while len(_flows) > MAX_PENDING:
        oldest = min(_flows, key=lambda k: _flows[k].created)
        _flows.pop(oldest, None)


def start(server_id: str, owner: Optional[str], redirect_uri: str) -> tuple[str, str]:
    """Begin one attempt; returns (state, S256 code challenge)."""
    _prune()
    flow = GoogleFlow(server_id=server_id, owner=owner or None,
                      verifier=new_verifier(), redirect_uri=redirect_uri)
    state = secrets.token_urlsafe(32)
    _flows[state] = flow
    return state, s256_challenge(flow.verifier)


def peek(state: str) -> Optional[GoogleFlow]:
    _prune()
    return _flows.get(state or "")


def _may_finish(flow: GoogleFlow, user: Optional[str], is_admin: bool) -> bool:
    if flow.owner is not None:
        return bool(user) and secrets.compare_digest(flow.owner, user)
    return bool(is_admin)


def take(state: str, user: Optional[str], is_admin: bool) -> Optional[GoogleFlow]:
    """Consume the attempt for `state` for its owner. None when unknown or
    expired; NotYours (attempt kept) when someone else started it."""
    flow = peek(state)
    if flow is None:
        return None
    if not _may_finish(flow, user, is_admin):
        raise NotYours()
    _flows.pop(state, None)
    return flow
