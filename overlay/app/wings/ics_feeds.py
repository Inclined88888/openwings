"""Calendar by link (patch 0025): subscribe to a calendar's .ics address.

Works with Google's "Secret address in iCal format", an Outlook.com or
Microsoft 365 published ICS link, an iCloud public calendar link, or any
.ics / webcal:// address. Read only: the events show in the Calendar as a
calendar of their own (CalendarCal.source == "ics"); upstream's calendar
routes and the assistant's calendar tool refuse to change them (patched in
routes/calendar_routes.py and src/tools/calendar.py), and the assistant's
calendar tools see them like any other calendar of that person.

Fetching is done on the server, carefully, because the link is typed by a
person and a secret link works like a password:

* http, https and webcal (read as https) only; no user name or password in
  the link.
* Every address the host resolves to is checked before connecting, and the
  connection goes to exactly those checked addresses (no second lookup that
  a rebinding name could change). Loopback, link-local, multicast,
  reserved, unspecified and shared (100.64/10) addresses are always refused;
  private ranges only when the existing private calendar setting
  ODYSSEUS_ALLOW_PRIVATE_CALDAV allows them (upstream's CalDAV rule).
* Redirects are followed by hand, at most five, and each new address is
  checked again the same way.
* At most 5 MB and 20 seconds in all: a watchdog closes the connection at
  the deadline, so a server that answers one byte at a time cannot hold a
  worker. At most two fetches per person and four in all run at once, and a
  person can add at most MAX_FEEDS_PER_PERSON calendars.
* The link is stored encrypted and never logged (only its host is).

Recurring events are expanded here, in each event's own time zone
(recurring_ical_events, which caldav already brings), so a weekly 09:00 in
Berlin stays at 09:00 after a daylight saving change; deleted (EXDATE),
moved (RECURRENCE-ID) and cancelled occurrences are applied. Each
occurrence is stored as one event, from a year back to two years ahead.

Refresh: every 30 minutes in the background, and on "Refresh now". Events
are replaced as a whole on each good fetch (a feed is the full calendar);
a failed fetch keeps the last good events and records a plain reason.
Removing the calendar removes its events.
"""
from __future__ import annotations

import asyncio
import hashlib
import http.client
import ipaddress
import json
import logging
import os
import re
import socket
import ssl
import threading
import time
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urljoin, urlsplit, urlunsplit

from wings.accounts import AccountError, register_model, test_hosts_allowed

logger = logging.getLogger(__name__)

MAX_BYTES = 5 * 1024 * 1024
TIMEOUT_SECONDS = 20.0
MAX_REDIRECTS = 5
REFRESH_SECONDS = 30 * 60
MAX_EVENTS = 5000
LOOKBACK_DAYS = 366
AHEAD_DAYS = 731
MAX_FEEDS_PER_PERSON = 25
MAX_FETCHES_PER_PERSON = 2
MAX_FETCHES = 4
PARSE_SECONDS = 15.0
USER_AGENT = "OpenWings-Calendar/1.0"
_SHARED_V4 = ipaddress.ip_network("100.64.0.0/10")
_COLORS = ("#4c8bf5", "#16a085", "#e67e22", "#8e44ad", "#c0392b", "#2c3e50")

# Per provider: short steps with links to the provider's own help pages.
PROVIDER_HELP = [
    {"key": "google", "name": "Google Calendar",
     "steps": "On a computer, open Google Calendar, then Settings. Under Settings for my calendars pick the calendar, choose Integrate calendar and copy the Secret address in iCal format.",
     "url": "https://support.google.com/calendar/answer/37648", "link": "Google's steps"},
    {"key": "outlook", "name": "Outlook.com or Microsoft 365",
     "steps": "In Outlook on the web open Calendar, then Settings, Shared calendars. Under Publish a calendar pick the calendar, set it to Can view all details, choose Publish and copy the ICS link.",
     "url": "https://support.microsoft.com/en-us/outlook/share-your-calendar-in-outlook-on-the-web", "link": "Microsoft's steps"},
    {"key": "icloud", "name": "iCloud",
     "steps": "On icloud.com/calendar, open the sharing settings next to the calendar, turn on Public Calendar and copy the link (it starts with webcal://).",
     "url": "https://support.apple.com/guide/icloud/share-a-calendar-mm6b1a9479/icloud", "link": "Apple's steps"},
    {"key": "other", "name": "Any other calendar",
     "steps": "Paste any address that ends in .ics or starts with webcal://."},
]


class FeedError(AccountError):
    pass


# ------------------------------------------------------------------ storage

def _feed_model():
    def make():
        from sqlalchemy import Column, DateTime, Integer, String, Text
        from core.database import Base, EncryptedText, utcnow_naive

        class WingsIcsFeed(Base):
            __tablename__ = "wings_ics_feeds"
            __table_args__ = {"extend_existing": True}
            id = Column(String, primary_key=True)
            owner = Column(String, nullable=False, index=True)
            calendar_id = Column(String, nullable=False, index=True)
            name = Column(String, nullable=False)
            url = Column(EncryptedText, nullable=False)
            host = Column(String, nullable=True)
            last_fetch = Column(Integer, nullable=True)
            last_ok = Column(Integer, nullable=True)
            last_error = Column(Text, nullable=True)
            event_count = Column(Integer, nullable=True)
            created_at = Column(DateTime, default=utcnow_naive)
        return WingsIcsFeed
    return register_model("ics_feed", make)


def _db():
    from core.database import SessionLocal
    return SessionLocal()


# ------------------------------------------------------------------ address checks

def private_allowed() -> bool:
    return os.environ.get("ODYSSEUS_ALLOW_PRIVATE_CALDAV", "0").strip().lower() in {"1", "true", "yes"}


def address_refusal(ip: ipaddress._BaseAddress) -> Optional[str]:
    """Why OpenWings will not fetch from `ip`, or None."""
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    if ip.is_loopback:
        return "it points at this computer"
    if ip.is_link_local:
        return "it points at a link-local address"
    if ip.is_multicast or ip.is_reserved or ip.is_unspecified:
        return "it points at a reserved address"
    if isinstance(ip, ipaddress.IPv4Address) and ip in _SHARED_V4:
        return "it points at a private network address"
    if ip.is_private and not private_allowed():
        return "it points at a private network address"
    return None


def normalize_url(raw: str) -> str:
    url = (raw or "").strip()
    if not url:
        raise FeedError("no_url", "Paste the calendar's address.")
    if len(url) > 2048:
        raise FeedError("bad_url", "That address is too long.")
    if url.lower().startswith("webcals://"):
        url = "https://" + url[len("webcals://"):]
    elif url.lower().startswith("webcal://"):
        url = "https://" + url[len("webcal://"):]
    parts = urlsplit(url)
    if parts.scheme.lower() not in ("http", "https"):
        raise FeedError("bad_scheme", "Use an address that starts with https://, http:// or webcal://.")
    if not parts.hostname:
        raise FeedError("bad_url", "That address has no server name.")
    if parts.username or parts.password:
        raise FeedError("bad_url", "Leave user names and passwords out of the address.")
    try:
        parts.port
    except ValueError:
        raise FeedError("bad_url", "That address has an invalid port.") from None
    return urlunsplit((parts.scheme.lower(), parts.netloc, parts.path or "/", parts.query, ""))


def resolve_checked(host: str, port: int, *, resolver=None) -> List[tuple]:
    """getaddrinfo results for host:port, all of them checked; FeedError if
    any address is refused (a name that mixes public and private answers is
    refused as a whole)."""
    host_l = host.lower().rstrip(".")
    if host_l in ("localhost",) or host_l.endswith(".localhost"):
        raise FeedError("blocked_address", "OpenWings does not fetch calendars from this computer's own addresses.")
    try:
        infos = (resolver or socket.getaddrinfo)(host, port, 0, socket.SOCK_STREAM)
    except OSError:
        raise FeedError("no_host", "That server name could not be found. Check the address.") from None
    if not infos:
        raise FeedError("no_host", "That server name could not be found. Check the address.")
    exempt = host_l in test_hosts_allowed()
    for info in infos:
        ip = ipaddress.ip_address(str(info[4][0]).split("%", 1)[0])
        why = None if exempt and not (ip.is_loopback or ip.is_link_local) else address_refusal(ip)
        if why:
            raise FeedError("blocked_address", f"OpenWings will not fetch that calendar: {why}.")
    return infos


class _PinnedHTTP(http.client.HTTPConnection):
    def __init__(self, host, port, infos, timeout):
        super().__init__(host, port, timeout=timeout)
        self._infos = infos

    def _open(self):
        last = None
        for family, socktype, proto, _canon, sockaddr in self._infos:
            sock = socket.socket(family, socktype, proto)
            try:
                sock.settimeout(self.timeout)
                sock.connect(sockaddr)
                return sock
            except OSError as exc:
                last = exc
                sock.close()
        raise last or OSError("no address")

    def connect(self):
        self.sock = self._open()


class _PinnedHTTPS(_PinnedHTTP):
    def __init__(self, host, port, infos, timeout):
        super().__init__(host, port, infos, timeout)
        self._ctx = ssl.create_default_context()

    def connect(self):
        self.sock = self._ctx.wrap_socket(self._open(), server_hostname=self.host)


_slots = threading.BoundedSemaphore(MAX_FETCHES)
_per_person: Dict[str, int] = {}
_per_person_lock = threading.Lock()

BUSY_MESSAGE = "OpenWings is loading other calendars right now. Try again in a minute."


class _Slot:
    """One of the few fetches allowed at a time (per person and in all)."""

    def __init__(self, owner: str, wait: float):
        self.owner = owner or ""
        self.wait = wait
        self.held = False

    def __enter__(self):
        with _per_person_lock:
            if _per_person.get(self.owner, 0) >= MAX_FETCHES_PER_PERSON:
                raise FeedError("busy", BUSY_MESSAGE)
            _per_person[self.owner] = _per_person.get(self.owner, 0) + 1
        if not _slots.acquire(timeout=max(0.0, self.wait)):
            self._release_person()
            raise FeedError("busy", BUSY_MESSAGE)
        self.held = True
        return self

    def _release_person(self):
        with _per_person_lock:
            n = _per_person.get(self.owner, 0) - 1
            if n > 0:
                _per_person[self.owner] = n
            else:
                _per_person.pop(self.owner, None)

    def __exit__(self, *exc):
        if self.held:
            _slots.release()
        self._release_person()
        return False


def _kill(conn, flag: threading.Event) -> None:
    """Watchdog: the deadline passed, end the connection now (a blocked read
    returns at once)."""
    flag.set()
    sock = getattr(conn, "sock", None)
    if sock is not None:
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass


def fetch(url: str, *, resolver=None, connection_factory=None, owner: str = "", wait: float = 5.0) -> Tuple[bytes, str]:
    """GET the calendar; returns (body, final URL). Raises FeedError.

    The whole fetch (every redirect, headers and body) ends after
    TIMEOUT_SECONDS: a watchdog shuts the socket at the deadline."""
    url = normalize_url(url)
    with _Slot(owner, wait):
        return _fetch(url, resolver=resolver, connection_factory=connection_factory)


def _fetch(url: str, *, resolver=None, connection_factory=None) -> Tuple[bytes, str]:
    deadline = time.monotonic() + TIMEOUT_SECONDS
    for _hop in range(MAX_REDIRECTS + 1):
        parts = urlsplit(url)
        https = parts.scheme == "https"
        port = parts.port or (443 if https else 80)
        infos = resolve_checked(parts.hostname, port, resolver=resolver)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise FeedError("timeout", "The calendar server took too long to answer.")
        if connection_factory is not None:
            conn = connection_factory(parts.hostname, port, infos, remaining, https)
        else:
            conn = (_PinnedHTTPS if https else _PinnedHTTP)(parts.hostname, port, infos, remaining)
        path = (parts.path or "/") + (("?" + parts.query) if parts.query else "")
        expired = threading.Event()
        watchdog = threading.Timer(remaining, _kill, args=(conn, expired))
        watchdog.daemon = True
        watchdog.start()
        try:
            conn.request("GET", path, headers={
                "User-Agent": USER_AGENT, "Accept": "text/calendar, */*;q=0.5",
                "Accept-Encoding": "identity", "Host": parts.netloc,
            })
            resp = conn.getresponse()
            status = resp.status
            if status in (301, 302, 303, 307, 308):
                location = resp.getheader("Location") or ""
                if not location:
                    raise FeedError("bad_redirect", "The calendar server sent a redirect without an address.")
                url = normalize_url(urljoin(url, location))
                continue
            if status == 401 or status == 403:
                raise FeedError("forbidden", "The calendar server refused the link. It may be private, or it was reset. Copy a fresh link.")
            if status == 404 or status == 410:
                raise FeedError("not_found", "No calendar at that link any more. Copy a fresh link.")
            if status != 200:
                raise FeedError("http_error", f"The calendar server answered with an error ({status}).")
            length = resp.getheader("Content-Length")
            if length and length.isdigit() and int(length) > MAX_BYTES:
                raise FeedError("too_big", "That calendar is larger than 5 MB, so OpenWings does not load it.")
            body = b""
            read = getattr(resp, "read1", None) or resp.read
            while True:
                if expired.is_set() or time.monotonic() > deadline:
                    raise FeedError("timeout", "The calendar server took too long to answer.")
                chunk = read(65536)
                if not chunk:
                    break
                body += chunk
                if len(body) > MAX_BYTES:
                    raise FeedError("too_big", "That calendar is larger than 5 MB, so OpenWings does not load it.")
            if expired.is_set():
                raise FeedError("timeout", "The calendar server took too long to answer.")
            return body, url
        except FeedError:
            raise
        except (socket.timeout, TimeoutError):
            raise FeedError("timeout", "The calendar server took too long to answer.") from None
        except ssl.SSLError:
            if expired.is_set():
                raise FeedError("timeout", "The calendar server took too long to answer.") from None
            raise FeedError("tls", "The calendar server's secure connection could not be checked.") from None
        except (OSError, http.client.HTTPException, ValueError):
            if expired.is_set():
                raise FeedError("timeout", "The calendar server took too long to answer.") from None
            raise FeedError("unreachable", "Could not reach the calendar server.") from None
        finally:
            watchdog.cancel()
            try:
                conn.close()
            except Exception:
                pass
    raise FeedError("too_many_redirects", "The link redirected too many times.")


# ------------------------------------------------------------------ parsing

def _utc_naive(value) -> Tuple[datetime, bool, bool]:
    """(naive datetime, all_day, is_utc) like upstream's CalDAV pull."""
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            return value.astimezone(timezone.utc).replace(tzinfo=None), False, True
        return value, False, False
    return datetime(value.year, value.month, value.day), True, False


def _load_calendar(body: bytes):
    from icalendar import Calendar
    try:
        return Calendar.from_ical(body)
    except Exception:
        raise FeedError("not_calendar", "That link did not return a calendar (.ics). Check you copied the iCal or ICS address.") from None


_FAST_FREQS = ("SECONDLY", "MINUTELY", "HOURLY")


def _tame_fast_rules(cal) -> None:
    """A series that repeats every second, minute or hour would have to be
    stepped through from its start to reach today; calendar apps do not make
    such series, so they show as one event (their first)."""
    for comp in cal.walk("VEVENT"):
        rule = comp.get("rrule")
        if rule is None:
            continue
        try:
            freq = [str(f).upper() for f in (rule.get("FREQ") or [])]
        except Exception:
            freq = []
        if any(f in _FAST_FREQS for f in freq):
            del comp["RRULE"]


def _event_dict(comp, feed_id: str, start, end, all_day: bool, is_utc: bool, occurrence_key: str) -> Dict[str, Any]:
    source_uid = str(comp.get("uid") or "") or uuid.uuid4().hex
    key = hashlib.sha256(f"{feed_id}\n{source_uid}\n{occurrence_key}".encode()).hexdigest()[:32]
    return {
        "uid": f"ics-{key}",
        "summary": str(comp.get("summary") or "")[:500],
        "description": str(comp.get("description") or "")[:20000],
        "location": str(comp.get("location") or "")[:500],
        "dtstart": start, "dtend": end, "all_day": all_day,
        "is_utc": is_utc and not all_day,
        "rrule": "", "exdates": [],
    }


def _times(comp) -> Optional[Tuple[datetime, datetime, bool, bool]]:
    start_p = comp.get("dtstart")
    if start_p is None:
        return None
    try:
        start, all_day, is_utc = _utc_naive(start_p.dt)
    except Exception:
        return None
    end_p = comp.get("dtend")
    try:
        if end_p is not None:
            end, _, _ = _utc_naive(end_p.dt)
        elif comp.get("duration") is not None:
            end = start + comp.get("duration").dt
        else:
            end = start + (timedelta(days=1) if all_day else timedelta(hours=1))
    except Exception:
        end = start + (timedelta(days=1) if all_day else timedelta(hours=1))
    if end <= start:
        end = start + (timedelta(days=1) if all_day else timedelta(minutes=1))
    return start, end, all_day, is_utc


def parse_events(body: bytes, feed_id: str, *, now: Optional[datetime] = None) -> Tuple[str, List[Dict[str, Any]]]:
    """(calendar name from X-WR-CALNAME, event dicts) using icalendar (the
    library upstream's CalDAV sync uses). Recurring series are expanded in
    their own time zone into single events, from LOOKBACK_DAYS back to
    AHEAD_DAYS ahead, with deleted, moved and cancelled occurrences
    applied."""
    cal = _load_calendar(body)
    name = str(cal.get("X-WR-CALNAME") or "").strip()[:80]
    try:
        import recurring_ical_events
    except ImportError:          # pragma: no cover (caldav brings it)
        return name, _parse_events_plain(cal, feed_id)
    _tame_fast_rules(cal)
    now = now or datetime.now(timezone.utc)
    start_at = now - timedelta(days=LOOKBACK_DAYS)
    end_at = now + timedelta(days=AHEAD_DAYS)
    out: List[Dict[str, Any]] = []
    seen = set()
    began = time.monotonic()
    try:
        occurrences = recurring_ical_events.of(cal, skip_bad_series=True).between(start_at, end_at)
        for comp in occurrences:
            if len(out) >= MAX_EVENTS or time.monotonic() - began > PARSE_SECONDS:
                break
            if str(comp.get("status") or "").upper() == "CANCELLED":
                continue
            t = _times(comp)
            if t is None:
                continue
            start, end, all_day, is_utc = t
            rid = comp.get("recurrence-id")
            occurrence = rid.to_ical().decode() if rid is not None else ""
            ev = _event_dict(comp, feed_id, start, end, all_day, is_utc, occurrence)
            if ev["uid"] in seen:
                continue
            seen.add(ev["uid"])
            out.append(ev)
    except FeedError:
        raise
    except Exception as exc:
        logger.info("Calendar link: recurring events not expanded (%s); showing them plainly", type(exc).__name__)
        return name, _parse_events_plain(_load_calendar(body), feed_id)
    out.sort(key=lambda e: e["dtstart"])
    return name, out


def _parse_events_plain(cal, feed_id: str) -> List[Dict[str, Any]]:
    """Fallback without recurrence expansion: each event once, series keep
    their RRULE for upstream's own expansion."""
    cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=LOOKBACK_DAYS)
    out: List[Dict[str, Any]] = []
    seen = set()
    overrides: Dict[str, List[str]] = {}
    masters: Dict[str, Optional[int]] = {}
    for comp in cal.walk("VEVENT"):
        if len(out) >= MAX_EVENTS:
            break
        start_p = comp.get("dtstart")
        if start_p is None:
            continue
        try:
            start, all_day, is_utc = _utc_naive(start_p.dt)
        except Exception:
            continue
        end_p = comp.get("dtend")
        try:
            if end_p is not None:
                end, _, _ = _utc_naive(end_p.dt)
            elif comp.get("duration") is not None:
                end = start + comp.get("duration").dt
            else:
                end = start + (timedelta(days=1) if all_day else timedelta(hours=1))
        except Exception:
            end = start + (timedelta(days=1) if all_day else timedelta(hours=1))
        if end <= start:
            end = start + (timedelta(days=1) if all_day else timedelta(minutes=1))
        rrule = comp.get("rrule").to_ical().decode() if comp.get("rrule") else ""
        source_uid = str(comp.get("uid") or "") or uuid.uuid4().hex
        rid = comp.get("recurrence-id")
        rid_key = rid.to_ical().decode() if rid is not None else ""
        if rid is not None:
            # A moved, changed or cancelled occurrence: hide the original
            # one in the series so it does not show twice (or at all).
            try:
                rv, rall, _ru = _utc_naive(rid.dt)
                overrides.setdefault(source_uid, []).append(
                    rv.strftime("%Y-%m-%d") if rall else rv.strftime("%Y-%m-%dT%H:%M"))
            except Exception:
                pass
        if not rrule and end < cutoff:
            continue
        if str(comp.get("status") or "").upper() == "CANCELLED":
            continue
        key = hashlib.sha256(f"{feed_id}\n{source_uid}\n{rid_key}".encode()).hexdigest()[:32]
        if key in seen:
            continue
        seen.add(key)
        exdates: List[str] = []
        for ex in (comp.get("exdate") if isinstance(comp.get("exdate"), list) else [comp.get("exdate")] if comp.get("exdate") else []):
            for d in getattr(ex, "dts", []):
                try:
                    v, _ad, _u = _utc_naive(d.dt)
                    exdates.append(v.strftime("%Y-%m-%d") if all_day else v.strftime("%Y-%m-%dT%H:%M"))
                except Exception:
                    continue
        masters[source_uid] = len(out) if not rid_key else masters.get(source_uid)
        out.append({
            "uid": f"ics-{key}",
            "summary": str(comp.get("summary") or "")[:500],
            "description": str(comp.get("description") or "")[:20000],
            "location": str(comp.get("location") or "")[:500],
            "dtstart": start, "dtend": end, "all_day": all_day,
            "is_utc": is_utc and not all_day,
            "rrule": rrule, "exdates": sorted(set(exdates)),
        })
    for source_uid, keys in overrides.items():
        index = masters.get(source_uid)
        if index is not None and out[index]["rrule"]:
            out[index]["exdates"] = sorted(set(out[index]["exdates"]) | set(keys))
    return out


# ------------------------------------------------------------------ store events

def _store(owner: str, feed_id: str, calendar_id: str, events: List[Dict[str, Any]]) -> int:
    from core.database import CalendarCal, CalendarEvent
    db = _db()
    try:
        cal = db.query(CalendarCal).filter(CalendarCal.id == calendar_id, CalendarCal.owner == owner).first()
        if cal is None:
            return 0
        db.query(CalendarEvent).filter(CalendarEvent.calendar_id == calendar_id).delete()
        for ev in events:
            db.add(CalendarEvent(
                uid=ev["uid"], calendar_id=calendar_id, summary=ev["summary"], description=ev["description"],
                location=ev["location"], dtstart=ev["dtstart"], dtend=ev["dtend"], all_day=ev["all_day"],
                is_utc=ev["is_utc"], rrule=ev["rrule"],
                recurrence_exdates=json.dumps(ev["exdates"]) if ev["exdates"] else "",
                origin="ics",
            ))
        db.commit()
        return len(events)
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def _record(feed_id: str, *, ok: bool, error: str = "", count: Optional[int] = None) -> None:
    Model = _feed_model()
    db = _db()
    try:
        row = db.get(Model, feed_id)
        if row is None:
            return
        now = int(time.time())
        row.last_fetch = now
        if ok:
            row.last_ok = now
            row.last_error = None
            row.event_count = count
        else:
            row.last_error = error[:300]
        db.commit()
    finally:
        db.close()


def refresh_feed(owner: str, feed_id: str, *, fetcher=None, wait: float = 5.0) -> Dict[str, Any]:
    Model = _feed_model()
    db = _db()
    try:
        row = db.query(Model).filter(Model.id == feed_id, Model.owner == owner).first()
        if row is None:
            raise FeedError("not_found", "That calendar is not subscribed.")
        url, calendar_id, host = row.url, row.calendar_id, row.host
    finally:
        db.close()
    try:
        body, _final = fetcher(url) if fetcher else fetch(url, owner=owner, wait=wait)
        _name, events = parse_events(body, feed_id)
        count = _store(owner, feed_id, calendar_id, events)
    except FeedError as exc:
        logger.info("Calendar link from %s not refreshed: %s", host or "?", exc.code)
        _record(feed_id, ok=False, error=exc.message)
        raise
    _record(feed_id, ok=True, count=count)
    return {"ok": True, "events": count}


def subscribe(owner: str, url: str, *, name: str = "", color: str = "", fetcher=None) -> Dict[str, Any]:
    """Check the link, load it once, then save the subscription."""
    url = normalize_url(url)
    if feed_count(owner) >= MAX_FEEDS_PER_PERSON:
        raise FeedError("too_many", f"You have {MAX_FEEDS_PER_PERSON} calendars added by link, the most OpenWings keeps. "
                        "Remove one first.")
    body, _final = fetcher(url) if fetcher else fetch(url, owner=owner)
    feed_id = uuid.uuid4().hex[:16]
    cal_name, events = parse_events(body, feed_id)
    name = (name or "").strip()[:80] or cal_name or (urlsplit(url).hostname or "Calendar")
    if not re.fullmatch(r"#[0-9a-fA-F]{6}", color or ""):
        color = _COLORS[int(hashlib.sha256(url.encode()).hexdigest(), 16) % len(_COLORS)]
    from core.database import CalendarCal
    Model = _feed_model()
    calendar_id = f"ics-{feed_id}"
    db = _db()
    try:
        db.add(CalendarCal(id=calendar_id, owner=owner, name=name, color=color.lower(), source="ics"))
        db.add(Model(id=feed_id, owner=owner, calendar_id=calendar_id, name=name, url=url,
                     host=(urlsplit(url).hostname or "")[:200]))
        db.commit()
    finally:
        db.close()
    count = _store(owner, feed_id, calendar_id, events)
    _record(feed_id, ok=True, count=count)
    return {"ok": True, "id": feed_id, "calendar_id": calendar_id, "name": name, "events": count}


def feed_count(owner: str) -> int:
    Model = _feed_model()
    db = _db()
    try:
        return db.query(Model).filter(Model.owner == owner).count()
    finally:
        db.close()


def unsubscribe(owner: str, feed_id: str) -> bool:
    from core.database import CalendarCal, CalendarEvent
    Model = _feed_model()
    db = _db()
    try:
        row = db.query(Model).filter(Model.id == feed_id, Model.owner == owner).first()
        if row is None:
            return False
        cal_id = row.calendar_id
        db.query(CalendarEvent).filter(CalendarEvent.calendar_id == cal_id).delete()
        cal = db.query(CalendarCal).filter(CalendarCal.id == cal_id, CalendarCal.owner == owner).first()
        if cal is not None:
            db.delete(cal)
        db.delete(row)
        db.commit()
        return True
    finally:
        db.close()


def on_calendar_deleted(owner: str, calendar_id: str) -> None:
    """Upstream's "delete calendar" removed an ICS calendar: forget its link
    too, so the next refresh does not bring it back."""
    if not str(calendar_id or "").startswith("ics-"):
        return
    Model = _feed_model()
    db = _db()
    try:
        db.query(Model).filter(Model.owner == owner, Model.calendar_id == calendar_id).delete()
        db.commit()
    finally:
        db.close()


def feeds_for(owner: str) -> List[Dict[str, Any]]:
    Model = _feed_model()
    from core.database import CalendarCal
    db = _db()
    try:
        out = []
        for r in db.query(Model).filter(Model.owner == owner).order_by(Model.created_at).all():
            cal = db.query(CalendarCal).filter(CalendarCal.id == r.calendar_id).first()
            out.append({"id": r.id, "name": cal.name if cal else r.name, "color": cal.color if cal else "",
                        "calendar_id": r.calendar_id, "host": r.host or "", "events": r.event_count or 0,
                        "last_ok": r.last_ok, "last_fetch": r.last_fetch, "error": r.last_error or ""})
        return out
    finally:
        db.close()


def is_read_only_calendar(cal) -> bool:
    return getattr(cal, "source", None) == "ics"


READ_ONLY_MESSAGE = ("This calendar is read only: it comes from a calendar link. "
                     "Change the event in the app that owns the calendar.")


# ------------------------------------------------------------------ background refresh

async def refresh_all_due(*, now: Optional[float] = None) -> int:
    Model = _feed_model()
    now = now or time.time()
    db = _db()
    try:
        due = [(r.owner, r.id) for r in db.query(Model).all()
               if not r.last_fetch or now - r.last_fetch >= REFRESH_SECONDS]
    finally:
        db.close()
    # A few at a time; each fetch ends after TIMEOUT_SECONDS whatever the
    # server does, so one slow calendar cannot hold up everyone else's.
    gate = asyncio.Semaphore(2)
    done = 0

    async def one(owner: str, feed_id: str) -> None:
        nonlocal done
        async with gate:
            try:
                await asyncio.wait_for(asyncio.to_thread(refresh_feed, owner, feed_id, wait=60.0),
                                       timeout=TIMEOUT_SECONDS + 60.0 + PARSE_SECONDS + 15.0)
                done += 1
            except FeedError:
                pass
            except Exception as exc:
                logger.warning("Calendar link refresh failed: %s", type(exc).__name__)

    await asyncio.gather(*(one(o, f) for o, f in due))
    return done


_task = {"t": None}


async def _loop():
    await asyncio.sleep(60)
    while True:
        try:
            await refresh_all_due()
        except Exception as exc:
            logger.warning("Calendar link refresh loop: %s", type(exc).__name__)
        await asyncio.sleep(300)


def start_background() -> None:
    if _task["t"] is not None:
        return
    try:
        _task["t"] = asyncio.get_running_loop().create_task(_loop())
    except RuntimeError:
        pass
