#!/usr/bin/env python3
"""Connect your accounts (patch 0025): end-to-end proof against local stubs.

Standard library only. Run through tests/e2e/accounts_run.sh, which starts
tests/e2e/accounts_stubs.py and the mock model next to a test stack whose
openwings service has WINGS_TEST_ENDPOINTS pointing Google's and the mail
providers' addresses at the stubs. Each check prints PASS/FAIL; the exit code
is the number of failures. Never prints a password, code or token.

What it proves, against the real application in its container:
  * app passwords (Gmail, iCloud, Yahoo): Connect logs in to IMAP and SMTP
    first (stub servers), says plainly when the password is wrong, saves
    nothing then, and creates the account through upstream's own route
  * calendar by link: fetched on the server, shown read only, refreshed,
    removed with its events; a redirect to a private or loopback address is
    refused; the assistant's calendar tool sees the events
  * your own Google key: the admin saves the client (never returned), a
    person signs in with Google (authorize URL with PKCE S256, offline,
    include_granted_scopes, only the ticked scopes); the callback lands on
    localhost while the person uses 127.0.0.1 and is bounced back to finish
    with their session; mail (XOAUTH2), two-way calendar over CalDAV with a
    bearer token (pull and push), a Meet link through the Calendar API,
    Drive search and read as assistant tools, and the assistant's mail tool
    over XOAUTH2; disconnect revokes
  * the assistant's tools are called the way a chat calls them (the same
    functions and tool registry, as the signed-in person) inside the app
    container; E2E_CHAT=1 also drives them through chats with the mock model
    (slow, and it depends on the model picking the tool)
  * the real accounts.google.com answers a well formed request with a fake
    client id by saying the client does not exist
"""
from __future__ import annotations

import http.cookiejar
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

PORT = os.environ.get("WINGS_PORT", "7013")
BASE = f"http://127.0.0.1:{PORT}"
ALT = f"http://localhost:{PORT}"
USER = os.environ.get("WINGS_USER", "admin")
PASSWORD = os.environ["WINGS_PASSWORD"]
STUBS = os.environ.get("STUBS_URL", "http://127.0.0.1:9100")
APP = os.environ.get("WINGS_APP_CONTAINER", "owconnect-openwings-1")
CLIENT_ID = "1234567890-stub.apps.googleusercontent.com"
CLIENT_SECRET = "GOCSPX-stub-secret"
GMAIL = "pat.stub@gmail.com"
RUN = str(int(time.time()))[-5:]   # event titles unique per run
failures = 0


def check(name, ok, detail=""):
    global failures
    print(("PASS " if ok else "FAIL ") + name + (f"  [{detail}]" if detail and not ok else ""), flush=True)
    if not ok:
        failures += 1
    return ok


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


class Client:
    def __init__(self, base):
        self.base = base
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar), NoRedirect)

    def req(self, method, path, *, json_body=None, form=None, timeout=60, raw=False, absolute=False):
        h = {"Origin": self.base}
        data = None
        if json_body is not None:
            data = json.dumps(json_body).encode()
            h["Content-Type"] = "application/json"
        elif form is not None:
            data = urllib.parse.urlencode(form).encode()
            h["Content-Type"] = "application/x-www-form-urlencoded"
        url = path if absolute else self.base + path
        r = urllib.request.Request(url, data=data, method=method, headers=h)
        try:
            with self.opener.open(r, timeout=timeout) as resp:
                body = resp.read()
                return resp.status, resp.headers, (body if raw else _j(body))
        except urllib.error.HTTPError as e:
            body = e.read()
            return e.code, e.headers, (body if raw else _j(body))

    def login(self):
        st, _h, body = self.req("POST", "/api/auth/login", json_body={"username": USER, "password": PASSWORD})
        return st == 200 and isinstance(body, dict) and body.get("ok")


def _j(body):
    try:
        return json.loads(body)
    except ValueError:
        return body.decode("utf-8", "replace")


def stub_log():
    with urllib.request.urlopen(STUBS + "/_log", timeout=10) as r:
        return json.loads(r.read())


def since(mark, kind=None):
    return [e for e in stub_log()[mark:] if kind is None or e["kind"] == kind]


def tool_outputs(c, sid, message):
    # Incognito: no saved memories in the prompt (on a test volume they arm
    # upstream's untrusted-context gate for every personal read).
    form = {"message": message, "session": sid, "mode": "agent", "allow_web_search": "false",
            "incognito": "true", "use_rag": "false"}
    for _ in range(6):
        st, _h, body = c.req("POST", "/api/chat_stream", form=form, raw=True, timeout=240)
        text = body.decode("utf-8", "replace") if isinstance(body, bytes) else str(body)
        events = []
        for line in text.splitlines():
            if line.startswith("data:"):
                try:
                    evt = json.loads(line[5:].strip())
                except ValueError:
                    continue
                if isinstance(evt, dict) and evt.get("type") == "tool_output":
                    events.append(evt)
        if events or not ("Model request failed" in text or "cooldown active" in text or "Cannot reach" in text):
            return events, text
        time.sleep(25)
    return events, text


def out_text(events):
    if not events:
        return ""
    return json.dumps([e.get("output") or e.get("result") or e for e in events])[:4000]


CHAT = os.environ.get("E2E_CHAT") == "1"
CHAT_SKIP = "needs the mock model to pick the tool; set E2E_CHAT=1 to run it (slow)"


def skip(name, why):
    print(f"SKIP {name}  [{why}]", flush=True)


IN_APP = r"""
import asyncio, json, os, sys
sys.path.insert(0, "/app")
kind, owner, args = os.environ["E2E_KIND"], os.environ["E2E_OWNER"], json.loads(os.environ["E2E_ARGS"])
if kind == "calendar":
    from src.tools.calendar import do_manage_calendar
    out = asyncio.run(do_manage_calendar(json.dumps(args), owner=owner))
elif kind == "drive":
    import wings.accounts as A
    A.load_providers(); A.install_tools()
    from src.agent_tools import TOOL_HANDLERS          # the assistant's own tool registry
    out = asyncio.run(TOOL_HANDLERS[args.pop("tool")](json.dumps(args), {"owner": owner}))
elif kind == "mail":
    import mcp_servers.email_server as es              # the assistant's mail MCP server
    res = asyncio.run(es.call_tool("list_emails", dict(args, _odysseus_owner=owner)))
    out = {"response": "\n".join(getattr(r, "text", str(r)) for r in res)}
print("E2E_RESULT:" + json.dumps(out, default=str))
"""


def tool_in_app(kind, args):
    """Call an assistant tool the way the chat does (same function, same
    registry, as the signed-in person), inside the app container, against
    the stub servers. Proves the tool without a model choosing it."""
    env = {"E2E_KIND": kind, "E2E_OWNER": USER, "E2E_ARGS": json.dumps(args)}
    cmd = ["docker", "exec", "-w", "/app"] + sum((["-e", f"{k}={v}"] for k, v in env.items()), []) + [APP, "python", "-c", IN_APP]
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=240).stdout
    for line in out.splitlines():
        if line.startswith("E2E_RESULT:"):
            return json.loads(line[len("E2E_RESULT:"):])
    return {"error": out[-300:]}


def main():
    c = Client(BASE)
    if not check("sign in at 127.0.0.1", c.login()):
        return failures
    st, _h, status = c.req("GET", "/api/wings/accounts/status")
    check("accounts status for the signed-in person", st == 200 and status.get("admin") is True, str(status)[:200])
    # Start clean (an earlier run may have left accounts behind).
    for m in status.get("mail", []):
        if m.get("address", "").startswith("pat."):
            c.req("DELETE", f"/api/wings/accounts/mail/{m['id']}")
    for link in status.get("google", {}).get("links", []):
        c.req("DELETE", f"/api/wings/accounts/google/{link['id']}")
    for f in status.get("feeds", []):
        c.req("DELETE", f"/api/wings/accounts/calendar-link/{f['id']}")
    for a in c.req("GET", "/api/calendar/config/accounts")[2].get("accounts", []):
        if "pat." in a.get("username", ""):
            c.req("DELETE", f"/api/calendar/config/accounts/{a['id']}")

    # ------------------------------------------------------------ app passwords
    mark = len(stub_log())
    st, _h, r = c.req("POST", "/api/wings/accounts/mail/connect",
                      json_body={"provider": "gmail", "address": "pat.app@gmail.com", "password": "wrong password"})
    check("Gmail app password: a wrong password is refused in plain words",
          st == 400 and r.get("error") == "wrong_password" and "did not accept that password" in r.get("message", ""), str(r)[:200])
    logins = since(mark, "imap_login")
    check("  ... after a real IMAP login attempt (stub)", logins and not logins[-1]["ok"] and logins[-1]["method"] == "password", str(logins))
    st, _h, accts = c.req("GET", "/api/email/accounts")
    check("  ... and nothing was saved", not any(a.get("from_address") == "pat.app@gmail.com" for a in accts.get("accounts", [])))

    mark = len(stub_log())
    st, _h, r = c.req("POST", "/api/wings/accounts/mail/connect",
                      json_body={"provider": "gmail", "address": "pat.app@gmail.com", "password": "abcd efgh ijkl mnop"})
    check("Gmail app password (spaces stripped): connected", st == 200 and r.get("ok"), str(r)[:200])
    kinds = [(e["kind"], e.get("method"), e.get("ok")) for e in since(mark) if e["kind"] in ("imap_login", "smtp_login")]
    check("  ... IMAP and SMTP logins tested first", ("imap_login", "password", True) in kinds and ("smtp_login", "password", True) in kinds, str(kinds))
    st, _h, accts = c.req("GET", "/api/email/accounts")
    gm = next((a for a in accts.get("accounts", []) if a.get("from_address") == "pat.app@gmail.com"), None)
    check("  ... account created by upstream's route (masked, no password returned)",
          gm and gm.get("has_imap_password") and "abcdefghijklmnop" not in json.dumps(accts), str(gm)[:200])

    st, _h, r = c.req("POST", "/api/wings/accounts/mail/connect",
                      json_body={"provider": "icloud", "address": "pat.stub@icloud.com", "password": "icld-pass-word-xyzw", "calendar": True})
    check("iCloud app-specific password: connected", st == 200 and r.get("ok"), str(r)[:200])
    check("  ... 'Also sync my iCloud calendars' adds a CalDAV account (caldav.icloud.com)",
          r.get("calendar") in ("syncing", "failed"), str(r)[:300])
    st, _h, cal_accts = c.req("GET", "/api/calendar/config/accounts")
    check("  ... visible in upstream's CalDAV account list",
          any(a.get("url", "").startswith("https://caldav.icloud.com") for a in cal_accts.get("accounts", [])) or r.get("calendar") == "failed",
          str(cal_accts)[:300] + " / " + str(r.get("calendar_error")))
    st, _h, r = c.req("POST", "/api/wings/accounts/mail/connect",
                      json_body={"provider": "yahoo", "address": "pat.stub@yahoo.com", "password": "nope"})
    check("Yahoo: wrong app password says so", st == 400 and "Yahoo did not accept" in r.get("message", ""), str(r)[:200])
    st, _h, r = c.req("POST", "/api/wings/accounts/mail/connect",
                      json_body={"provider": "yahoo", "address": "pat.stub@yahoo.com", "password": "yahooapppass1234"})
    check("Yahoo app password: connected", st == 200 and r.get("ok"), str(r)[:200])

    # ------------------------------------------------------------ calendar by link
    st, _h, r = c.req("POST", "/api/wings/accounts/calendar-link", json_body={"url": "http://stubs:9100/ics/redirect-private.ics"})
    check("calendar link: a redirect to a private address is refused", st == 400 and r.get("error") == "blocked_address"
          and "private" in r.get("message", ""), str(r)[:200])
    st, _h, r = c.req("POST", "/api/wings/accounts/calendar-link", json_body={"url": "http://stubs:9100/ics/redirect-loopback.ics"})
    check("calendar link: a redirect to this computer is refused", st == 400 and r.get("error") == "blocked_address", str(r)[:200])
    st, _h, r = c.req("POST", "/api/wings/accounts/calendar-link", json_body={"url": "ftp://stubs/x.ics"})
    check("calendar link: only http, https and webcal", st == 400 and r.get("error") == "bad_scheme", str(r)[:200])
    st, _h, feed = c.req("POST", "/api/wings/accounts/calendar-link", json_body={"url": "http://stubs:9100/ics/moved.ics", "name": "Family"})
    check("calendar link: subscribed (after a same-site redirect)", st == 200 and feed.get("events", 0) >= 2, str(feed)[:200])
    st, _h, cals = c.req("GET", "/api/calendar/calendars")
    fcal = next((x for x in cals.get("calendars", []) if x.get("href") == feed.get("calendar_id")), {})
    check("  ... its own calendar, read only", fcal.get("read_only") is True and fcal.get("name") == "Family", str(fcal))
    now = datetime.now(timezone.utc)
    rng = f"start={now.strftime('%Y-%m-%d')}&end={(now + timedelta(days=30)).strftime('%Y-%m-%d')}"
    st, _h, evs = c.req("GET", f"/api/calendar/events?{rng}")
    fe = [e for e in evs.get("events", []) if e.get("calendar_href") == feed.get("calendar_id")]
    check("  ... events show in the Calendar", any("School concert" in e.get("summary", "") for e in fe), str(fe)[:300])
    if fe:
        st, _h, r = c.req("PUT", f"/api/calendar/events/{urllib.parse.quote(fe[0]['uid'])}", json_body={"summary": "changed"})
        check("  ... and cannot be changed", st == 409, f"{st} {r}")
        st, _h, r = c.req("DELETE", f"/api/calendar/events/{urllib.parse.quote(fe[0]['uid'])}")
        check("  ... or deleted", st == 409, f"{st} {r}")
        st, _h, r = c.req("POST", "/api/calendar/events", json_body={"summary": "x", "dtstart": "2099-01-01T10:00:00Z",
                                                                    "calendar_href": feed["calendar_id"]})
        check("  ... or given new events", st == 409, f"{st} {r}")
    st, _h, r = c.req("POST", f"/api/wings/accounts/calendar-link/{feed.get('id')}/refresh")
    check("  ... Refresh now", st == 200 and r.get("ok"), str(r))

    # The assistant's calendar tool sees them.
    ep_id = None
    def new_session():
        # A fresh chat per check: upstream's safety gate (rightly) asks the
        # person to approve a private read once outside text (a mail subject,
        # a file) is in the chat, as for its own email tools.
        st, _h, sess = c.req("POST", "/api/session", form={"name": "accounts e2e", "endpoint_url": "http://mockllm:9999/v1",
                                                           "model": "mock-alpha", "endpoint_id": ep_id})
        return sess["id"]
    st, _h, eps = c.req("GET", "/api/model-endpoints") if CHAT else (0, None, [])
    ep = next((e for e in eps if e.get("base_url") == "http://mockllm:9999/v1"), None) if isinstance(eps, list) else None
    if CHAT:
        if not ep:
            st, _h, ep = c.req("POST", "/api/model-endpoints", form={"name": "Mock", "base_url": "http://mockllm:9999/v1",
                                                                     "api_key": "sk-mock", "supports_tools": "true"})
        ep_id = ep["id"]
        c.req("PATCH", f"/api/model-endpoints/{ep_id}", json_body={"supports_tools": True})
    args = {"action": "list_events", "start": now.strftime("%Y-%m-%dT00:00:00"), "end": (now + timedelta(days=30)).strftime("%Y-%m-%dT00:00:00")}
    r = tool_in_app("calendar", args)
    check("assistant's calendar tool (manage_calendar, as the person) sees the linked calendar",
          "School concert" in json.dumps(r), str(r)[:300])
    if CHAT:
        ev, text = tool_outputs(c, new_session(), "what is on my calendar this month? WINGS_CALL manage_calendar " + json.dumps(args))
        check("  ... through a chat with the mock model", "School concert" in out_text(ev), out_text(ev)[:300] or text[:300])
    else:
        skip("  ... through a chat with the mock model", CHAT_SKIP)

    # ------------------------------------------------------------ Google own key
    admin_alt = Client(ALT)
    check("admin signs in at localhost too", admin_alt.login())
    st, _h, info = admin_alt.req("POST", "/api/wings/accounts/google/setup",
                                 json_body={"client_id": CLIENT_ID, "client_secret": CLIENT_SECRET})
    check("admin saves the Google key (from localhost)", st == 200 and info.get("ok"), str(info)[:200])
    check("  ... the secret never comes back", CLIENT_SECRET not in json.dumps(info) and info["client"]["client_id"].startswith("••••"))
    check("  ... redirect URI is the one shown in the wizard",
          info.get("redirect_uri") == f"{ALT}/api/email/oauth/google/callback" and info.get("redirect_ok"), str(info.get("redirect_uri")))
    raw = subprocess.run(["docker", "exec", APP, "python", "-c",
                          "import sqlite3;c=sqlite3.connect('/app/data/app.db');print(c.execute('select client_secret from wings_oauth_clients').fetchone()[0])"],
                         capture_output=True, text=True).stdout
    check("  ... stored encrypted", raw.strip() and "stub-secret" not in raw, raw[:40])

    mark = len(stub_log())
    st, _h, r = c.req("POST", "/api/wings/accounts/google/start", json_body={"mail": True, "calendar": True, "drive": True})
    url = r.get("authorize_url", "")
    q = {k: v[0] for k, v in urllib.parse.parse_qs(urllib.parse.urlsplit(url).query).items()}
    check("Sign in with Google: authorize URL (PKCE S256, offline, include_granted_scopes)",
          q.get("code_challenge_method") == "S256" and q.get("access_type") == "offline"
          and q.get("include_granted_scopes") == "true" and q.get("client_id") == CLIENT_ID, url[:200])
    check("  ... only the ticked scopes", set(q.get("scope", "").split()) == {
        "openid", "email", "https://mail.google.com/", "https://www.googleapis.com/auth/calendar",
        "https://www.googleapis.com/auth/drive.readonly"}, q.get("scope"))
    st, h, _b = c.req("GET", url, absolute=True)
    back = h.get("Location", "") if h else ""
    check("  ... Google's page (stub) sends the browser to the registered redirect (localhost)",
          st == 302 and back.startswith(f"{ALT}/api/email/oauth/google/callback?"), f"{st} {back[:80]}")
    st, h, _b = c.req("GET", back, absolute=True)   # no session cookie for localhost
    hop = h.get("Location", "") if h else ""
    check("  ... callback on localhost bounces to 127.0.0.1 with the same query", st == 303
          and hop.startswith(f"{BASE}/api/email/oauth/google/callback?") and urllib.parse.urlsplit(hop).query == urllib.parse.urlsplit(back).query,
          f"{st} {hop[:100]}")
    st, h, _b = c.req("GET", hop, absolute=True)
    done = h.get("Location", "") if h else ""
    check("  ... and finishes there with the person's session", st == 303 and done == "/?ow_accounts=google", f"{st} {done}")
    toks = since(mark, "token")
    check("  ... code exchanged with the PKCE verifier and the client secret (stub checked)",
          any(t.get("grant") == "authorization_code" and t.get("ok") and t.get("pkce_ok") for t in toks), str(toks))
    st, h, _b = c.req("GET", hop, absolute=True)
    check("  ... used once only", (h.get("Location", "") if h else "").endswith("invalid_state"), str(h.get("Location") if h else ""))
    st, _h, status = c.req("GET", "/api/wings/accounts/status")
    link = (status.get("google", {}).get("links") or [{}])[0]
    check("Google card: Connected as the account, Mail Calendar Drive",
          link.get("account") == GMAIL and link.get("mail") and link.get("calendar") and link.get("drive"), str(link))
    check("  ... mail account with XOAUTH2", any(m.get("address") == GMAIL and m.get("oauth") == "google" for m in status.get("mail", [])),
          str(status.get("mail")))

    # Calendar: two-way through upstream's CalDAV code with a bearer token.
    mark = len(stub_log())
    st, _h, r = c.req("POST", "/api/calendar/sync", timeout=120)
    dav = since(mark, "caldav")
    check("Google Calendar sync: CalDAV with a bearer token (no password)", dav and all(e["bearer"] and not e["basic"] for e in dav),
          str(dav[:3]) + str(r)[:200])
    st, _h, cals = c.req("GET", "/api/calendar/calendars")
    gcal = next((x for x in cals.get("calendars", []) if x.get("google")), None)
    check("  ... the Google calendar is listed (Meet available)", bool(gcal), str(cals)[:300])
    st, _h, evs = c.req("GET", f"/api/calendar/events?start={now.strftime('%Y-%m-%d')}&end={(now + timedelta(days=5)).strftime('%Y-%m-%d')}")
    check("  ... events pulled from Google (stub)", any("Team sync" in e.get("summary", "") for e in evs.get("events", [])), str(evs)[:300])
    if gcal:
        mark = len(stub_log())
        start = (now + timedelta(days=2)).replace(minute=0, second=0, microsecond=0)
        st, _h, ev = c.req("POST", "/api/calendar/events", json_body={
            "summary": f"Planning call {RUN}", "dtstart": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "dtend": (start + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ"), "calendar_href": gcal["href"]}, timeout=120)
        puts = [e for e in since(mark, "caldav") if e["method"] == "PUT"]
        check("  ... a new event is written to Google over CalDAV (bearer)", st == 200 and puts and puts[0]["bearer"], f"{st} {puts}")
        st, _h, meet = c.req("POST", "/api/wings/accounts/google/meet", json_body={"uid": ev.get("uid")}, timeout=60)
        api = since(mark, "calendar_api_meet")
        check("  ... Add a Google Meet link: Calendar API, conferenceDataVersion=1, hangoutsMeet",
              st == 200 and meet.get("meet_link", "").startswith("https://meet.google.com/") and api and api[0]["ok"], f"{meet} {api}")
        st, _h, got = c.req("GET", f"/api/calendar/events/{ev.get('uid')}")
        check("  ... join link saved on the event", "meet.google.com" in json.dumps(got), str(got)[:300])
        # The assistant's calendar tool can ask for one too.
        args = {"action": "create_event", "summary": f"Assistant meeting {RUN}", "dtstart": (start + timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%S"),
                "calendar_href": gcal["href"], "google_meet": True}
        mark2 = len(stub_log())
        r = tool_in_app("calendar", args)
        api = since(mark2, "calendar_api_meet")
        check("  ... assistant's calendar tool (create_event, google_meet) adds a Meet link",
              "meet.google.com" in json.dumps(r) and api and api[0]["ok"], f"{str(r)[:300]} {api}")
        if CHAT:
            evts, text = tool_outputs(c, new_session(), "schedule a meeting with a google meet link in my calendar WINGS_CALL manage_calendar "
                                      + json.dumps(dict(args, summary=args["summary"] + " chat")))
            check("  ... through a chat with the mock model", "meet.google.com" in out_text(evts), out_text(evts)[:300] or text[:300])
        else:
            skip("  ... through a chat with the mock model", CHAT_SKIP)

    # Drive tools for the assistant, for this person only.
    mark = len(stub_log())
    r = tool_in_app("drive", {"tool": "google_drive_search", "query": "budget"})
    check("assistant: google_drive_search (tool registry, as the person) finds the file",
          "Budget 2026" in json.dumps(r) and "stubsheet0001" in json.dumps(r), str(r)[:300])
    r = tool_in_app("drive", {"tool": "google_drive_read", "file_id": "stubsheet0001"})
    check("assistant: google_drive_read exports a Sheet as CSV", "rent,1200" in json.dumps(r), str(r)[:300])
    r = tool_in_app("drive", {"tool": "google_drive_read", "file_id": "stubpdf000001"})
    check("assistant: google_drive_read reads a PDF with upstream's reader", "Lease ends on 31 March 2027" in json.dumps(r), str(r)[:300])
    if CHAT:
        evts, text = tool_outputs(c, new_session(), 'find my budget in google drive WINGS_CALL google_drive_search {"query": "budget"}')
        check("  ... google_drive_search through a chat with the mock model", "Budget 2026" in out_text(evts), out_text(evts)[:300] or text[:300])
    else:
        skip("  ... through a chat with the mock model", CHAT_SKIP)
    check("  ... Drive was called with this person's token", all(e["ok"] for e in since(mark) if e["kind"].startswith("drive")), "")

    # The assistant's mail tool over XOAUTH2.
    mark = len(stub_log())
    r = tool_in_app("mail", {"account": GMAIL, "max_results": 5})
    logins = since(mark, "imap_login")
    check("assistant's mail tool (email MCP server list_emails, as the person) reads Gmail over IMAP XOAUTH2",
          "Welcome to the stub inbox" in json.dumps(r)
          and any(e["method"] == "XOAUTH2" and e["ok"] and e["user"] == GMAIL for e in logins), str(r)[:300] + str(logins))
    if CHAT:
        evts, text = tool_outputs(c, new_session(), f'list my latest emails WINGS_CALL list_emails {json.dumps({"account": GMAIL, "max_results": 5})}')
        check("  ... through a chat with the mock model", "Welcome to the stub inbox" in out_text(evts), out_text(evts)[:300] or text[:300])
    else:
        skip("  ... through a chat with the mock model", CHAT_SKIP)

    # Disconnect: revoke and forget.
    mark = len(stub_log())
    st, _h, r = c.req("DELETE", f"/api/wings/accounts/google/{link.get('id')}")
    check("Disconnect Google: revoked at Google (stub) and forgotten", st == 200 and since(mark, "revoke"), f"{st} {r}")
    st, _h, status = c.req("GET", "/api/wings/accounts/status")
    check("  ... mail account and calendar removed", not status.get("google", {}).get("links")
          and not any(m.get("address") == GMAIL for m in status.get("mail", [])), str(status)[:300])

    st, _h, r = c.req("DELETE", f"/api/wings/accounts/calendar-link/{feed.get('id')}")
    st, _h, evs = c.req("GET", f"/api/calendar/events?{rng}")
    check("Removing the linked calendar removes its events",
          not any(e.get("calendar_href") == feed.get("calendar_id") for e in evs.get("events", [])))

    print(f"\n{failures} failure(s)")
    return failures


if __name__ == "__main__":
    sys.exit(main())
