#!/usr/bin/env python3
"""Local stand-ins for the services Connect your accounts talks to (patch
0025), for the end-to-end proof in tests/e2e/accounts_e2e.py. Test only:
nothing here is used by OpenWings itself.

Runs in the Radicale image (tomsquest/docker-radicale, /venv/bin/python has
Radicale 3), one process, four servers:

  :9100  HTTP  Google's sign-in (accounts.google.com authorize, which checks
               PKCE S256 and the parameters OpenWings sends; oauth2.googleapis
               token and revoke), OpenID userinfo, the Calendar API (events
               by iCalUID, Meet links through conferenceData.createRequest),
               Drive (search, metadata, export, download), and calendar
               feeds (/ics/...: a real .ics, a redirect to a private address,
               a redirect to loopback).
  :9101  CalDAV  Radicale behind a wrapper that accepts only Bearer tokens
               this stub issued (Google's CalDAV accepts only OAuth), at
               Google's path /caldav/v2/<calendar id>/events.
  :1143  IMAP  LOGIN with app passwords and AUTHENTICATE XOAUTH2 with issued
               tokens; SELECT/EXAMINE, SEARCH, FETCH of two messages.
  :1025  SMTP  AUTH PLAIN/LOGIN (app passwords) and AUTH XOAUTH2.

GET :9100/_log returns everything the stubs saw (no secrets), for the proof.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
import socketserver
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlencode, urlsplit

CLIENT_ID = os.environ.get("STUB_CLIENT_ID", "1234567890-stub.apps.googleusercontent.com")
CLIENT_SECRET = os.environ.get("STUB_CLIENT_SECRET", "GOCSPX-stub-secret")
GOOGLE_EMAIL = os.environ.get("STUB_GOOGLE_EMAIL", "pat.stub@gmail.com")
APP_PASSWORDS = {
    "pat.app@gmail.com": "abcdefghijklmnop",
    "pat.stub@icloud.com": "icld-pass-word-xyzw",
    "pat.stub": "icld-pass-word-xyzw",
    "pat.stub@yahoo.com": "yahooapppass1234",
}
ROOT = Path(os.environ.get("STUB_DAV_ROOT", "/tmp/stubdav"))

LOG: list = []
CODES: dict = {}
TOKENS: dict = {}          # access token -> email
REFRESH: dict = {}         # refresh token -> (email, scope)
LOCK = threading.Lock()


def log(kind, **data):
    with LOCK:
        LOG.append({"t": round(time.time(), 3), "kind": kind, **data})


def issue(email):
    tok = "ya29.stub-" + secrets.token_urlsafe(12)
    with LOCK:
        TOKENS[tok] = email
    return tok


# ------------------------------------------------------------------ HTTP

ICS = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//OpenWings stub//EN
X-WR-CALNAME:Stub family calendar
BEGIN:VEVENT
UID:stub-feed-1@example.test
DTSTART:{d1}T150000Z
DTEND:{d1}T160000Z
SUMMARY:School concert (from a link)
LOCATION:Town hall
END:VEVENT
BEGIN:VEVENT
UID:stub-feed-2@example.test
DTSTART;VALUE=DATE:{d2}
SUMMARY:Grandma visits
RRULE:FREQ=WEEKLY;COUNT=3
END:VEVENT
END:VCALENDAR
"""


def ics_body():
    t = time.gmtime(time.time() + 2 * 86400)
    t2 = time.gmtime(time.time() + 3 * 86400)
    return ICS.format(d1=time.strftime("%Y%m%d", t), d2=time.strftime("%Y%m%d", t2)).replace("\n", "\r\n").encode()


def pdf_bytes(text):
    """A one-page PDF with `text` (enough for pypdf to read)."""
    content = f"BT /F1 18 Tf 72 720 Td ({text}) Tj ET".encode()
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length " + str(len(content)).encode() + b" >>\nstream\n" + content + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = b"%PDF-1.4\n"
    offsets = []
    for i, o in enumerate(objs, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + o + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return out


DRIVE_FILES = {
    "stubsheet0001": {"id": "stubsheet0001", "name": "Budget 2026", "mimeType": "application/vnd.google-apps.spreadsheet",
                      "modifiedTime": "2026-11-01T10:00:00Z"},
    "stubpdf000001": {"id": "stubpdf000001", "name": "Flat lease.pdf", "mimeType": "application/pdf", "size": "900",
                      "modifiedTime": "2026-10-01T10:00:00Z"},
}


def find_event(email, uid):
    base = ROOT / "collection-root" / email / "events"
    for f in base.glob("*.ics"):
        if f"UID:{uid}" in f.read_text(errors="replace"):
            return f
    return None


class Http(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, status, body=b"", ctype="application/json", headers=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _bearer(self):
        auth = self.headers.get("Authorization", "")
        tok = auth[7:] if auth.startswith("Bearer ") else ""
        return TOKENS.get(tok)

    def _form(self):
        n = int(self.headers.get("Content-Length") or 0)
        return {k: v[0] for k, v in parse_qs(self.rfile.read(n).decode()).items()}

    def do_GET(self):
        parts = urlsplit(self.path)
        q = {k: v[0] for k, v in parse_qs(parts.query).items()}
        p = parts.path
        if p == "/_log":
            return self._send(200, LOG)
        if p == "/accounts/o/oauth2/v2/auth":
            need = ["client_id", "redirect_uri", "response_type", "scope", "state", "code_challenge", "code_challenge_method"]
            missing = [k for k in need if not q.get(k)]
            problems = missing[:]
            if q.get("client_id") != CLIENT_ID:
                problems.append("client_id")
            if q.get("code_challenge_method") != "S256" or q.get("response_type") != "code":
                problems.append("pkce/response_type")
            if q.get("access_type") != "offline" or q.get("include_granted_scopes") != "true":
                problems.append("offline/include_granted_scopes")
            log("authorize", scope=q.get("scope"), redirect_uri=q.get("redirect_uri"), problems=problems,
                access_type=q.get("access_type"), include_granted_scopes=q.get("include_granted_scopes"))
            if problems:
                return self._send(400, {"error": "invalid_request", "problems": problems})
            code = "4/stub-" + secrets.token_urlsafe(10)
            granted = q["scope"]
            if os.environ.get("STUB_DROP_SCOPE"):
                granted = " ".join(s for s in granted.split() if s != os.environ["STUB_DROP_SCOPE"])
            CODES[code] = {"challenge": q["code_challenge"], "redirect_uri": q["redirect_uri"], "scope": granted}
            loc = q["redirect_uri"] + "?" + urlencode({"state": q["state"], "code": code, "scope": granted})
            return self._send(302, b"", headers={"Location": loc})
        if p == "/oidc/v1/userinfo":
            email = self._bearer()
            log("userinfo", ok=bool(email))
            if not email:
                return self._send(401, {"error": "invalid_token"})
            return self._send(200, {"sub": "1001", "email": email, "email_verified": True, "name": "Pat Stub"})
        m = re.match(r"^/googleapis/calendar/v3/calendars/([^/]+)/events$", p)
        if m:
            email = self._bearer()
            cal = unquote(m.group(1))
            hit = find_event(email or "", q.get("iCalUID", "")) if email == cal else None
            log("calendar_api_list", ok=bool(email), calendar=cal, found=bool(hit))
            if not email:
                return self._send(401, {"error": {"code": 401}})
            items = [{"id": "gevt" + hashlib.sha1(q["iCalUID"].encode()).hexdigest()[:10], "iCalUID": q["iCalUID"]}] if hit else []
            return self._send(200, {"items": items})
        m = re.match(r"^/googleapis/calendar/v3/calendars/([^/]+)/events/([^/]+)$", p)
        if m:
            return self._send(200, {"id": m.group(2), "hangoutLink": "https://meet.google.com/stb-abcd-efg"})
        if p == "/googleapis/drive/v3/files":
            email = self._bearer()
            log("drive_search", ok=bool(email), q=q.get("q"))
            if not email:
                return self._send(401, {"error": {"code": 401}})
            words = re.findall(r"contains '([^']*)'", q.get("q", ""))
            hits = [f for f in DRIVE_FILES.values() if any(w.lower() in f["name"].lower() for w in words)] or list(DRIVE_FILES.values())
            return self._send(200, {"files": hits})
        m = re.match(r"^/googleapis/drive/v3/files/([A-Za-z0-9_-]+)(/export)?$", p)
        if m:
            email = self._bearer()
            f = DRIVE_FILES.get(m.group(1))
            log("drive_read", ok=bool(email), file=m.group(1), export=bool(m.group(2)), alt=q.get("alt"), mime=q.get("mimeType"))
            if not email:
                return self._send(401, {"error": {"code": 401}})
            if not f:
                return self._send(404, {"error": {"code": 404}})
            if m.group(2):
                return self._send(200, b"item,amount\nrent,1200\nfood,450\n", "text/csv")
            if q.get("alt") == "media":
                return self._send(200, pdf_bytes("Lease ends on 31 March 2027"), "application/pdf")
            return self._send(200, f)
        if p == "/ics/family.ics":
            log("ics", path=p)
            return self._send(200, ics_body(), "text/calendar")
        if p == "/ics/moved.ics":
            log("ics", path=p)
            return self._send(302, b"", headers={"Location": "/ics/family.ics"})
        if p == "/ics/redirect-private.ics":
            log("ics", path=p)
            return self._send(302, b"", headers={"Location": "http://10.20.30.40/secret.ics"})
        if p == "/ics/redirect-loopback.ics":
            log("ics", path=p)
            return self._send(302, b"", headers={"Location": "http://127.0.0.1:7000/api/health"})
        return self._send(404, {"error": "not found", "path": p})

    def do_POST(self):
        p = urlsplit(self.path).path
        if p == "/oauth2/token":
            f = self._form()
            if f.get("client_id") != CLIENT_ID or f.get("client_secret") != CLIENT_SECRET:
                log("token", grant=f.get("grant_type"), ok=False, why="client")
                return self._send(401, {"error": "invalid_client"})
            if f.get("grant_type") == "authorization_code":
                c = CODES.pop(f.get("code", ""), None)
                verifier = f.get("code_verifier", "")
                challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
                ok = bool(c) and challenge == c["challenge"] and f.get("redirect_uri") == c["redirect_uri"]
                log("token", grant="authorization_code", ok=ok, pkce_ok=bool(c) and challenge == c["challenge"])
                if not ok:
                    return self._send(400, {"error": "invalid_grant"})
                rt = "1//stub-refresh-" + secrets.token_urlsafe(8)
                REFRESH[rt] = (GOOGLE_EMAIL, c["scope"])
                return self._send(200, {"access_token": issue(GOOGLE_EMAIL), "refresh_token": rt, "expires_in": 3599,
                                        "scope": c["scope"], "token_type": "Bearer"})
            if f.get("grant_type") == "refresh_token":
                hit = REFRESH.get(f.get("refresh_token", ""))
                log("token", grant="refresh_token", ok=bool(hit))
                if not hit:
                    return self._send(400, {"error": "invalid_grant"})
                return self._send(200, {"access_token": issue(hit[0]), "expires_in": 3599, "scope": hit[1], "token_type": "Bearer"})
            return self._send(400, {"error": "unsupported_grant_type"})
        if p == "/oauth2/revoke":
            f = self._form()
            REFRESH.pop(f.get("token", ""), None)
            log("revoke", ok=True)
            return self._send(200, {})
        return self._send(404, {"error": "not found"})

    def do_PATCH(self):
        parts = urlsplit(self.path)
        q = {k: v[0] for k, v in parse_qs(parts.query).items()}
        m = re.match(r"^/googleapis/calendar/v3/calendars/([^/]+)/events/([^/]+)$", parts.path)
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        email = self._bearer()
        req = ((body.get("conferenceData") or {}).get("createRequest") or {})
        ok = bool(m and email and q.get("conferenceDataVersion") == "1"
                  and (req.get("conferenceSolutionKey") or {}).get("type") == "hangoutsMeet" and req.get("requestId"))
        log("calendar_api_meet", ok=ok, conferenceDataVersion=q.get("conferenceDataVersion"),
            solution=(req.get("conferenceSolutionKey") or {}).get("type"))
        if not ok:
            return self._send(400, {"error": {"code": 400}})
        return self._send(200, {"id": m.group(2), "hangoutLink": "https://meet.google.com/stb-abcd-efg",
                                "conferenceData": {"createRequest": {"status": {"statusCode": "success"}},
                                                   "entryPoints": [{"entryPointType": "video", "uri": "https://meet.google.com/stb-abcd-efg"}]}})


# ------------------------------------------------------------------ CalDAV (Radicale, Bearer only)

def make_caldav():
    import radicale
    from radicale import config as rconfig
    coll = ROOT / "collection-root" / GOOGLE_EMAIL / "events"
    coll.mkdir(parents=True, exist_ok=True)
    (coll / ".Radicale.props").write_text(json.dumps({"tag": "VCALENDAR", "D:displayname": GOOGLE_EMAIL}))
    t = time.gmtime(time.time() + 86400)
    day = time.strftime("%Y%m%d", t)
    (coll / "stub-google-1.ics").write_text(
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//stub//EN\r\nBEGIN:VEVENT\r\nUID:stub-google-1\r\n"
        f"DTSTART:{day}T100000Z\r\nDTEND:{day}T110000Z\r\nSUMMARY:Team sync (in Google Calendar)\r\n"
        "END:VEVENT\r\nEND:VCALENDAR\r\n")
    conf = rconfig.load()
    conf.update({"auth": {"type": "http_x_remote_user"}, "rights": {"type": "owner_only"},
                 "storage": {"filesystem_folder": str(ROOT)}}, "stub", privileged=True)
    app = radicale.Application(conf)

    def wrapper(environ, start_response):
        path = environ.get("PATH_INFO", "")
        auth = environ.get("HTTP_AUTHORIZATION", "")
        email = TOKENS.get(auth[7:]) if auth.startswith("Bearer ") else None
        log("caldav", method=environ.get("REQUEST_METHOD"), path=path, bearer=bool(email),
            basic=auth.startswith("Basic "))
        if not path.startswith("/caldav/v2/"):
            start_response("404 Not Found", [("Content-Type", "text/plain")])
            return [b"not found"]
        if not email:
            start_response("401 Unauthorized", [("WWW-Authenticate", 'Bearer realm="stub"'), ("Content-Type", "text/plain")])
            return [b"bearer token required"]
        environ = dict(environ)
        environ["PATH_INFO"] = path[len("/caldav/v2"):]
        environ["SCRIPT_NAME"] = ""
        environ["HTTP_X_SCRIPT_NAME"] = "/caldav/v2"
        environ["HTTP_X_REMOTE_USER"] = email
        environ.pop("HTTP_AUTHORIZATION", None)
        return app(environ, start_response)
    return wrapper


def serve_caldav():
    from wsgiref.simple_server import WSGIRequestHandler, WSGIServer, make_server

    class Quiet(WSGIRequestHandler):
        def log_message(self, *a):
            pass

    class Threaded(socketserver.ThreadingMixIn, WSGIServer):
        daemon_threads = True

    make_server("0.0.0.0", 9101, make_caldav(), server_class=Threaded, handler_class=Quiet).serve_forever()


# ------------------------------------------------------------------ IMAP and SMTP

def xoauth2_user(b64):
    try:
        raw = base64.b64decode(b64).decode()
    except Exception:
        return None, None
    m = re.match(r"^user=([^\x01]*)\x01auth=Bearer ([^\x01]*)\x01\x01$", raw)
    if not m:
        return None, None
    return m.group(1), m.group(2)


MESSAGES = [
    b"From: Ana <ana@example.test>\r\nTo: you\r\nSubject: Welcome to the stub inbox\r\nDate: Mon, 09 Nov 2026 09:00:00 +0000\r\nMessage-ID: <m1@stub>\r\n\r\nHello!\r\n",
    b"From: Bo <bo@example.test>\r\nTo: you\r\nSubject: Quarterly budget\r\nDate: Tue, 10 Nov 2026 09:00:00 +0000\r\nMessage-ID: <m2@stub>\r\n\r\nNumbers attached.\r\n",
]


class Imap(socketserver.StreamRequestHandler):
    def w(self, line):
        self.wfile.write(line if isinstance(line, bytes) else line.encode())

    def handle(self):
        self.w("* OK [CAPABILITY IMAP4rev1 AUTH=PLAIN AUTH=XOAUTH2] Stub IMAP ready\r\n")
        user = None
        while True:
            line = self.rfile.readline()
            if not line:
                return
            text = line.decode(errors="replace").rstrip("\r\n")
            parts = text.split(" ", 2)
            if len(parts) < 2:
                continue
            tag, cmd = parts[0], parts[1].upper()
            arg = parts[2] if len(parts) > 2 else ""
            if cmd == "UID":
                sub = arg.split(" ", 1)
                cmd, arg = "UID " + sub[0].upper(), (sub[1] if len(sub) > 1 else "")
            if cmd == "CAPABILITY":
                self.w(f"* CAPABILITY IMAP4rev1 AUTH=PLAIN AUTH=XOAUTH2\r\n{tag} OK done\r\n")
            elif cmd == "LOGIN":
                toks = re.findall(r'"((?:[^"\\]|\\.)*)"|(\S+)', arg)
                vals = [a.replace('\\"', '"').replace("\\\\", "\\") if a else b for a, b in toks]
                ok = len(vals) >= 2 and APP_PASSWORDS.get(vals[0]) == vals[1]
                log("imap_login", user=vals[0] if vals else "", method="password", ok=ok)
                if ok:
                    user = vals[0]
                    self.w(f"{tag} OK LOGIN completed\r\n")
                else:
                    self.w(f"{tag} NO [AUTHENTICATIONFAILED] Invalid credentials (Failure)\r\n")
            elif cmd == "AUTHENTICATE":
                mech, _, initial = arg.partition(" ")
                if mech.upper() != "XOAUTH2":
                    self.w(f"{tag} NO unsupported\r\n")
                    continue
                if not initial:
                    self.w("+ \r\n")
                    initial = self.rfile.readline().decode().strip()
                who, tok = xoauth2_user(initial)
                ok = bool(who) and TOKENS.get(tok) == who
                log("imap_login", user=who, method="XOAUTH2", ok=ok)
                if ok:
                    user = who
                    self.w(f"{tag} OK AUTHENTICATE completed\r\n")
                else:
                    self.w(f"{tag} NO [AUTHENTICATIONFAILED] Invalid credentials (Failure)\r\n")
            elif cmd in ("SELECT", "EXAMINE"):
                if not user:
                    self.w(f"{tag} NO not authenticated\r\n")
                    continue
                self.w(f"* {len(MESSAGES)} EXISTS\r\n* 0 RECENT\r\n* OK [UIDVALIDITY 7] ok\r\n* FLAGS (\\Seen \\Answered)\r\n"
                       f"{tag} OK [{'READ-ONLY' if cmd == 'EXAMINE' else 'READ-WRITE'}] {cmd} completed\r\n")
            elif cmd == "LIST":
                self.w('* LIST (\\HasNoChildren) "/" "INBOX"\r\n* LIST (\\HasNoChildren \\Sent) "/" "Sent"\r\n'
                       f"{tag} OK LIST completed\r\n")
            elif cmd in ("SEARCH", "UID SEARCH"):
                log("imap_search", user=user)
                self.w(f"* SEARCH {' '.join(str(i + 1) for i in range(len(MESSAGES)))}\r\n{tag} OK SEARCH completed\r\n")
            elif cmd in ("FETCH", "UID FETCH"):
                seqset, _, what = arg.partition(" ")
                ids = []
                for piece in seqset.split(","):
                    if ":" in piece:
                        a, b = piece.split(":")
                        b = len(MESSAGES) if b == "*" else int(b)
                        ids.extend(range(int(a), b + 1))
                    elif piece.isdigit():
                        ids.append(int(piece))
                for i in ids:
                    if not 1 <= i <= len(MESSAGES):
                        continue
                    msg = MESSAGES[i - 1]
                    head = msg.split(b"\r\n\r\n", 1)[0] + b"\r\n\r\n"
                    items = [f"UID {i}", "FLAGS ()"]
                    blob = b""
                    up = what.upper()
                    if "HEADER" in up:
                        key = "RFC822.HEADER" if "RFC822.HEADER" in up else "BODY[HEADER]"
                        blob = head
                    elif "RFC822" in up or "BODY" in up:
                        key = "RFC822" if "RFC822" in up else "BODY[]"
                        blob = msg
                    if blob:
                        self.w(f"* {i} FETCH ({' '.join(items)} {key} {{{len(blob)}}}\r\n".encode() + blob + b")\r\n")
                    else:
                        self.w(f"* {i} FETCH ({' '.join(items)})\r\n")
                self.w(f"{tag} OK FETCH completed\r\n")
            elif cmd == "LOGOUT":
                self.w(f"* BYE bye\r\n{tag} OK LOGOUT completed\r\n")
                return
            else:
                self.w(f"{tag} OK {cmd} completed\r\n")


class Smtp(socketserver.StreamRequestHandler):
    def w(self, line):
        self.wfile.write(line.encode())

    def handle(self):
        self.w("220 stub ESMTP\r\n")
        while True:
            line = self.rfile.readline()
            if not line:
                return
            text = line.decode(errors="replace").rstrip("\r\n")
            up = text.upper()
            if up.startswith(("EHLO", "HELO")):
                self.w("250-stub\r\n250-AUTH PLAIN LOGIN XOAUTH2\r\n250 OK\r\n")
            elif up.startswith("AUTH PLAIN"):
                b64 = text[10:].strip()
                if not b64:
                    self.w("334 \r\n")
                    b64 = self.rfile.readline().decode().strip()
                try:
                    _z, user, pw = base64.b64decode(b64).decode().split("\x00")
                except Exception:
                    user, pw = "", ""
                ok = APP_PASSWORDS.get(user) == pw
                log("smtp_login", user=user, method="password", ok=ok)
                self.w("235 ok\r\n" if ok else "535 5.7.8 Username and Password not accepted\r\n")
            elif up.startswith("AUTH LOGIN"):
                self.w("334 VXNlcm5hbWU6\r\n")
                user = base64.b64decode(self.rfile.readline().strip()).decode()
                self.w("334 UGFzc3dvcmQ6\r\n")
                pw = base64.b64decode(self.rfile.readline().strip()).decode()
                ok = APP_PASSWORDS.get(user) == pw
                log("smtp_login", user=user, method="password", ok=ok)
                self.w("235 ok\r\n" if ok else "535 5.7.8 Username and Password not accepted\r\n")
            elif up.startswith("AUTH XOAUTH2"):
                who, tok = xoauth2_user(text[12:].strip())
                ok = bool(who) and TOKENS.get(tok) == who
                log("smtp_login", user=who, method="XOAUTH2", ok=ok)
                self.w("235 ok\r\n" if ok else "535 5.7.8 bad token\r\n")
            elif up.startswith("DATA"):
                self.w("354 go\r\n")
                while self.rfile.readline().rstrip(b"\r\n") != b".":
                    pass
                log("smtp_data")
                self.w("250 queued\r\n")
            elif up.startswith("QUIT"):
                self.w("221 bye\r\n")
                return
            else:
                self.w("250 OK\r\n")


class TS(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def main():
    threading.Thread(target=lambda: TS(("0.0.0.0", 1143), Imap).serve_forever(), daemon=True).start()
    threading.Thread(target=lambda: TS(("0.0.0.0", 1025), Smtp).serve_forever(), daemon=True).start()
    threading.Thread(target=serve_caldav, daemon=True).start()
    srv = ThreadingHTTPServer(("0.0.0.0", 9100), Http)
    srv.daemon_threads = True
    print("stubs ready", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
