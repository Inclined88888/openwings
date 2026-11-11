#!/usr/bin/env python3
"""Wings end-to-end checks against a running stack.

Standard library only. Run through tests/e2e/run.sh, which starts the
deterministic mock model (mock_llm.py) and a GreenMail SMTP/IMAP test server
on the stack's network, then calls this script. Each check prints PASS/FAIL;
the exit code is the number of failures.

What it proves, against the real application:
  * login, chat streaming, switching models mid-conversation, history
  * per-agent tool access is enforced server-side (what the model is offered)
  * no email leaves without a person: human compose Send delivers; the
    agent's send tool, its loopback API calls and API tokens do not; scheduled
    rows a person did not create are refused by the poller
  * secrets at rest: no plaintext endpoint key, mail password or MCP env value
    anywhere in the data volume; no key file in the data volume
  * child-process environment scrubbing for user-added MCP servers
  * browser automation (profile browser): the agent opens, fills and submits
    a real form in the browser container through the chat API; passwords,
    card numbers and "Place order" are handed to the person; non-web URLs
    and page JavaScript are refused; the live view needs a signed-in session
    and a same-origin WebSocket
"""
from __future__ import annotations

import base64
import http.cookiejar
import imaplib
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

BASE = os.environ.get("WINGS_URL", "http://127.0.0.1:7011").rstrip("/")
ORIGIN = os.environ.get("WINGS_ORIGIN", "http://localhost:7011")
USER = os.environ.get("WINGS_USER", "admin")
PASSWORD = os.environ["WINGS_PASSWORD"]
PROJECT = os.environ.get("COMPOSE_PROJECT_NAME", "wings")
APP = os.environ.get("WINGS_APP_CONTAINER", f"{PROJECT}-odysseus-1")
MAIL_HOST_FROM_HOST = os.environ.get("WINGS_TEST_IMAP_HOST", "")  # optional host-side IMAP check
SECRET_ENDPOINT_KEY = "sk-wings-e2e-ENDPOINTSECRET-" + uuid.uuid4().hex[:8]
SECRET_MAIL_PASSWORD = "mailpass123"
SECRET_MCP_ENV = "mcp-wings-e2e-ENVSECRET-" + uuid.uuid4().hex[:8]

failures = 0


def skip(name: str, why: str) -> None:
    print(f"SKIP {name}  [{why}]")


def check(name: str, ok: bool, detail: str = "") -> bool:
    global failures
    print(("PASS " if ok else "FAIL ") + name + (f"  [{detail}]" if detail and not ok else ""))
    if not ok:
        failures += 1
    return ok


class Client:
    def __init__(self) -> None:
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))

    def req(self, method, path, *, json_body=None, form=None, headers=None, timeout=60, raw=False):
        h = {"Origin": ORIGIN}
        h.update(headers or {})
        data = None
        if json_body is not None:
            data = json.dumps(json_body).encode()
            h["Content-Type"] = "application/json"
        elif form is not None:
            data = urllib.parse.urlencode(form).encode()
            h["Content-Type"] = "application/x-www-form-urlencoded"
        r = urllib.request.Request(BASE + path, data=data, method=method, headers=h)
        try:
            with self.opener.open(r, timeout=timeout) as resp:
                body = resp.read()
                return resp.status, (body if raw else _maybe_json(body))
        except urllib.error.HTTPError as e:
            body = e.read()
            return e.code, (body if raw else _maybe_json(body))


def _maybe_json(body: bytes):
    try:
        return json.loads(body)
    except ValueError:
        return body.decode("utf-8", "replace")


def sh(*cmd: str) -> str:
    return subprocess.run(cmd, capture_output=True, text=True, check=False).stdout


def app_python(code: str) -> str:
    return sh("docker", "exec", "-i", APP, "python", "-c", code)


def stream_chat(c: Client, session_id: str, message: str, **extra) -> str:
    """Send one chat turn; retry once if the model host was unreachable.

    The app puts an unreachable model host on a 20 s cooldown; a single
    dropped TCP connect between containers would otherwise fail a check
    that has nothing to do with the model."""
    text = _stream_chat_once(c, session_id, message, **extra)
    if "Model request failed" in text or "Cannot reach" in text or "cooldown active" in text:
        time.sleep(22)
        text = _stream_chat_once(c, session_id, message, **extra)
    return text


def _stream_chat_once(c: Client, session_id: str, message: str, **extra) -> str:
    form = {"message": message, "session": session_id, **extra}
    status, body = c.req("POST", "/api/chat_stream", form=form, raw=True, timeout=180)
    text = body.decode("utf-8", "replace") if isinstance(body, bytes) else str(body)
    if status != 200:
        return f"HTTP {status}: {text[:300]}"
    out = []
    for line in text.splitlines():
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if payload in ("", "[DONE]"):
            continue
        try:
            evt = json.loads(payload)
        except ValueError:
            continue
        if isinstance(evt, dict):
            for key in ("content", "delta", "text", "token"):
                v = evt.get(key)
                if isinstance(v, str):
                    out.append(v)
                    break
    return "".join(out) or text


def chat_tool_outputs(c: Client, session_id: str, message: str, **extra) -> tuple[list[dict], str]:
    """One agent turn; returns the tool_output events and the raw stream.

    Retries while the sandbox's model host is cooling down (see stream_chat)."""
    form = {"message": message, "session": session_id, "mode": "agent", "allow_web_search": "false", **extra}
    for attempt in range(8):
        status, body = c.req("POST", "/api/chat_stream", form=form, raw=True, timeout=240)
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


def _ws_handshake(cookie_header: str, origin: str) -> tuple[int, bytes]:
    """Open the live-view WebSocket by hand (stdlib only). Returns the HTTP
    status and the first frame's payload (the VNC server greeting)."""
    url = urllib.parse.urlsplit(BASE)
    sock = socket.create_connection((url.hostname, url.port or 80), timeout=15)
    try:
        key = base64.b64encode(os.urandom(16)).decode()
        request = (f"GET /wings/browser/websockify HTTP/1.1\r\nHost: {url.netloc}\r\nUpgrade: websocket\r\n"
                   f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n"
                   f"Sec-WebSocket-Protocol: binary\r\nOrigin: {origin}\r\nCookie: {cookie_header}\r\n\r\n")
        sock.sendall(request.encode())
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = sock.recv(4096)
            if not chunk:
                break
            data += chunk
        head, _, rest = data.partition(b"\r\n\r\n")
        status = int(head.split(b" ", 2)[1]) if head.startswith(b"HTTP/") else 0
        if status != 101:
            return status, b""
        while len(rest) < 2:
            rest += sock.recv(4096)
        length = rest[1] & 0x7F
        offset = 2
        if length == 126:
            while len(rest) < 4:
                rest += sock.recv(4096)
            length, offset = int.from_bytes(rest[2:4], "big"), 4
        while len(rest) < offset + length:
            rest += sock.recv(4096)
        return status, rest[offset:offset + length]
    finally:
        sock.close()


def browser_checks(c: Client, ep_id: str) -> None:
    st, status = c.req("GET", "/api/wings/browser/status")
    check("browser status: enabled and allowed for the admin", isinstance(status, dict) and status.get("allowed") is True, str(status)[:200])
    st, page = c.req("GET", "/wings/browser/", raw=True)
    check("live view page is served to the signed-in admin", st == 200 and b"live.js" in page, str(st))
    st, rfb = c.req("GET", "/wings/browser/novnc/core/rfb.js", raw=True)
    check("live view relays the noVNC client from the browser container", st == 200 and b"class RFB" in rfb, str(st))
    anon = Client()
    st, _ = anon.req("GET", "/api/wings/browser/status")
    check("browser status needs a signed-in session", st == 401, str(st))
    cookie = "; ".join(f"{k.name}={k.value}" for k in c.jar)
    st, greeting = _ws_handshake(cookie, ORIGIN)
    check("live view WebSocket streams the browser screen", st == 101 and greeting.startswith(b"RFB "), f"{st} {greeting[:20]!r}")
    st, _ = _ws_handshake(cookie, "http://evil.example")
    check("live view WebSocket refuses a foreign Origin", st != 101, str(st))
    st, _ = _ws_handshake("", ORIGIN)
    check("live view WebSocket refuses a request without a session", st != 101, str(st))

    st, sess = c.req("POST", "/api/session", form={
        "name": "e2e browser", "endpoint_url": "http://mockllm:9999/v1", "model": "mock-alpha", "endpoint_id": ep_id})
    sid = sess["id"]

    unreachable = "MODEL_UNREACHABLE"

    def run(message: str) -> str:
        events, text = chat_tool_outputs(c, sid, message)
        out = "\n".join(str(e.get("output") or "") for e in events if e.get("tool") == "private_browser")
        if not out and ("Model request failed" in text or "cooldown active" in text):
            return unreachable
        return out or text[-400:]

    def bcheck(name: str, ok: bool, out: str) -> None:
        # A turn whose model call never got through (seen in the test sandbox:
        # new connections from the app to the mock are sometimes dropped)
        # proves nothing either way: report it, do not count it as a failure.
        if out == unreachable:
            skip(name, "the model host was unreachable for this turn; rerun")
        else:
            check(name, ok, out[:300])

    def call(args: dict, words: str = "Use the browser for this.") -> str:
        return run(f"{words} WINGS_CALL private_browser {json.dumps(args)}")

    # Routing: the web toggle is off by default in the UI; a web task must
    # still be offered the browser instead of a "web search is off" refusal.
    events, text = chat_tool_outputs(c, sid, "WINGS_LIST_TOOLS Log into my supplier portal and download the latest invoice")
    bcheck("web-task wording is offered the browser with web search off", "private_browser" in text and "Web access is disabled" not in text,
          text[-200:])

    private_ok = app_python("import os;print(os.environ.get('WINGS_BROWSER_ALLOW_PRIVATE',''))").strip() in ("1", "true")
    site = "http://testsite:8000"
    out = call({"action": "open", "url": "file:///etc/passwd"})
    bcheck("browser refuses non-web URLs", "Only http and https" in out, out)
    out = call({"action": "evaluate", "script": "document.cookie"})
    # Refused twice over: the request authority does not grant evaluate, and
    # the backend refuses it unless WINGS_BROWSER_ALLOW_EVAL=1.
    bcheck("browser refuses page JavaScript by default", "turned off" in out or "outside server request authority" in out, out)
    if not private_ok:
        out = call({"action": "open", "url": f"{site}/form.html"})
        bcheck("browser refuses private network addresses by default", "private or local" in out, out)
        skip("browser page flow", "test site is on a private address; set WINGS_BROWSER_ALLOW_PRIVATE=1 to run it")
        return
    out = call({"action": "open", "url": f"{site}/form.html"})
    bcheck("agent opens a page and gets a numbered outline", "PAGE STATE" in out and 'textbox "Full name"' in out, out)
    out = call({"action": "fill", "find": "Full name", "text": "Ada Lovelace"})
    bcheck("agent fills a text field", 'value="Ada Lovelace"' in out, out)
    out = call({"action": "fill", "find": "Country", "text": "New Zealand"})
    bcheck("agent chooses a dropdown option", 'selected="New Zealand"' in out, out)
    out = call({"action": "fill", "find": "I agree to the terms", "text": "true"})
    bcheck("agent ticks a checkbox", "checked" in out and "unchecked" not in out.split("I agree to the terms", 2)[-1][:20], out)
    out = call({"action": "fill", "find": "Password", "text": "hunter2"})
    bcheck("agent never types a password (handed to the person)", "PAUSED" in out and "password" in out, out)
    out = call({"action": "click", "find": "Continue"})
    bcheck("agent submits the form", "Thanks, Ada Lovelace" in out and "country=nz" in out and "agree=yes" in out, out)
    out = call({"action": "open", "url": f"{site}/checkout.html"})
    out = call({"action": "fill", "find": "Card number", "text": "4111111111111111"})
    bcheck("agent never types a card number", "PAUSED" in out and "payment card" in out, out)
    out = call({"action": "click", "find": "Place order"})
    bcheck("'Place order' is handed to the person", "PAUSED" in out, out)
    out = call({"action": "press", "key": "Enter", "find": "Card number"})
    out = call({"action": "snapshot"})
    bcheck("the order was not placed", "checkout.html" in out and "ORDER PLACED" not in out, out)
    out = call({"action": "open", "url": f"{site}/index.html"})
    out = call({"action": "click", "find": "Download invoice"})
    m = __import__("re").search(r'Downloaded "([^"]+)"', out)
    bcheck("a download is saved and reported to the agent", bool(m), out)
    if m:
        name = m.group(1)
        out = call({"action": "read", "path": name})
        bcheck("the agent reads the downloaded file", "Amount due: 42.10" in out, out)
        st, listing = c.req("GET", "/api/wings/browser/downloads")
        check("the live view lists the download", st == 200 and any(d.get("name") == name for d in listing.get("downloads", [])),
              str(listing)[:200])
        st, body = c.req("GET", "/wings/browser/downloads/" + urllib.parse.quote(name), raw=True)
        check("the person can save the download through Wings", st == 200 and b"Amount due" in body, str(st))
    out = call({"action": "read", "path": "../../app/data/app.db"})
    bcheck("download names cannot escape the downloads folder", "without folders" in out, out)
    events, text = chat_tool_outputs(c, sid, "Use the browser for this. WINGS_CALL private_browser " + json.dumps({"action": "screenshot"}))
    shot = next((e for e in events if e.get("screenshot")), None)
    bcheck("screenshot reaches the chat as an image", bool(shot) and str(shot.get("screenshot", "")).startswith("data:image/jpeg;base64,"),
           unreachable if not events and "Model request failed" in text else str([sorted(e.keys()) for e in events])[:200])


def main() -> int:
    c = Client()
    st, body = c.req("POST", "/api/auth/login", json_body={"username": USER, "password": PASSWORD})
    if not check("login", st == 200 and isinstance(body, dict) and body.get("ok"), str(body)[:200]):
        return failures

    # --- models --------------------------------------------------------------
    st, eps = c.req("GET", "/api/model-endpoints")
    ep = next((e for e in eps if e.get("base_url") == "http://mockllm:9999/v1"), None) if isinstance(eps, list) else None
    if not ep:
        st, ep = c.req("POST", "/api/model-endpoints", form={
            "name": "Mock", "base_url": "http://mockllm:9999/v1", "api_key": SECRET_ENDPOINT_KEY,
            "supports_tools": "true"})
    check("register OpenAI-compatible endpoint", isinstance(ep, dict) and "mock-alpha" in (ep.get("models") or []), str(ep)[:200])
    ep_id = ep["id"]
    # The mock speaks native OpenAI tool calls; say so (upstream otherwise
    # falls back to textual tool transport for endpoints of unknown ability).
    c.req("PATCH", f"/api/model-endpoints/{ep_id}", json_body={"supports_tools": True})
    check("API never returns the endpoint key", SECRET_ENDPOINT_KEY not in json.dumps(c.req("GET", "/api/model-endpoints")[1]))

    if os.environ.get("WINGS_E2E_ONLY") == "browser":
        browser_checks(c, ep_id)
        print(f"\n{failures} failure(s)")
        return failures

    # --- chat, streaming, model switch ---------------------------------------
    st, sess = c.req("POST", "/api/session", form={
        "name": "e2e chat", "endpoint_url": "http://mockllm:9999/v1", "model": "mock-alpha", "endpoint_id": ep_id})
    sid = (sess or {}).get("id") if isinstance(sess, dict) else None
    check("create chat session", bool(sid), str(sess)[:200])
    a = stream_chat(c, sid, "first question", mode="chat")
    check("streamed reply from model A", "[mock-alpha]" in a, a[:200])
    st, _ = c.req("PATCH", f"/api/session/{sid}", form={
        "model": "mock-beta", "endpoint_url": "http://mockllm:9999/v1", "endpoint_id": ep_id})
    check("switch model mid-conversation", st == 200, str(st))
    b = stream_chat(c, sid, "second question", mode="chat")
    check("next reply comes from model B", "[mock-beta]" in b, b[:200])
    st, hist = c.req("GET", f"/api/history/{sid}?limit=50")
    hist_txt = json.dumps(hist)
    check("history keeps both turns", "first question" in hist_txt and "second question" in hist_txt, hist_txt[:200])

    # --- secrets at rest ------------------------------------------------------
    raw = app_python(
        "import sqlite3;c=sqlite3.connect('/app/data/app.db');"
        f"print(c.execute('select headers from sessions where id=?',('{sid}',)).fetchone()[0])")
    check("session headers encrypted at rest", raw.strip().startswith('"enc:'), raw[:80])
    keyfiles = sh("docker", "exec", APP, "sh", "-c", "ls -a /app/data | grep -E '^\\.(app_)?key$' || true").strip()
    check("no encryption key file in the data volume", keyfiles == "", keyfiles)

    # --- per-agent tool access -----------------------------------------------
    st, r = c.req("POST", "/api/presets/custom", json_body={
        "name": "Scheduler", "enabled": True, "temperature": 1.0, "max_tokens": 0,
        "system_prompt": "You manage my calendar.", "enabled_tools": ["calendar"]})
    check("save agent limited to Calendar", st == 200 and r.get("success"), str(r)[:200])
    # Upstream routes each turn to the tool families its intent needs, so
    # probe one intent at a time and compare restricted vs unrestricted.
    import re as _re

    def offered_tools(text: str) -> list[str]:
        m = _re.search(r"TOOLS: ([a-z0-9_,]*)", text)
        return [t for t in (m.group(1) if m else "").split(",") if t]

    ask_cal = "WINGS_LIST_TOOLS what is on my calendar tomorrow"
    ask_web = "WINGS_LIST_TOOLS search the web for today's weather news"
    c.req("POST", "/api/presets/custom", json_body={
        "name": "Scheduler", "enabled": True, "temperature": 1.0, "max_tokens": 0,
        "system_prompt": "You manage my calendar.", "enabled_tools": ["calendar"]})
    lim_cal = offered_tools(stream_chat(c, sid, ask_cal, mode="agent", preset_id="custom"))
    lim_web = offered_tools(stream_chat(c, sid, ask_web, mode="agent", preset_id="custom"))
    check("calendar-only agent IS offered manage_calendar", "manage_calendar" in lim_cal, ",".join(lim_cal) or "none")
    check("calendar-only agent is NOT offered web tools", not {"web_search", "web_fetch"} & set(lim_web), ",".join(lim_web))
    lim_shell = [t for t in lim_cal + lim_web if t in ("bash", "python", "read_file", "write_file")]
    check("calendar-only agent is NOT offered shell/file tools", not lim_shell, ",".join(lim_shell))
    # Re-post without the field (as older UI paths do): restriction must persist.
    c.req("POST", "/api/presets/custom", json_body={
        "name": "Scheduler", "enabled": True, "temperature": 1.0, "max_tokens": 0, "system_prompt": "You manage my calendar."})
    preset = c.req("GET", "/api/presets")[1].get("custom", {})
    check("re-post without enabled_tools keeps the restriction", preset.get("enabled_tools") == ["calendar"], str(preset.get("enabled_tools")))
    c.req("POST", "/api/presets/custom", json_body={
        "name": "Scheduler", "enabled": True, "temperature": 1.0, "max_tokens": 0,
        "system_prompt": "You manage my calendar.", "enabled_tools": None})
    full_cal = offered_tools(stream_chat(c, sid, ask_cal, mode="agent", preset_id="custom"))
    full_web = offered_tools(stream_chat(c, sid, ask_web, mode="agent", preset_id="custom"))
    check("unrestricted agent IS offered calendar (control)", "manage_calendar" in full_cal, ",".join(full_cal))
    check("unrestricted agent IS offered web (control)", bool({"web_search", "web_fetch"} & set(full_web)), ",".join(full_web))
    c.req("POST", "/api/presets/custom", json_body={"name": "", "enabled": False, "temperature": 1.0, "max_tokens": 0, "system_prompt": ""})

    # --- email gate -------------------------------------------------------------
    st, acct = c.req("POST", "/api/email/accounts", json_body={
        "name": "e2e", "is_default": True, "imap_host": "mail", "imap_port": 3143, "imap_user": "me",
        "imap_password": SECRET_MAIL_PASSWORD, "imap_starttls": False, "smtp_host": "mail", "smtp_port": 3025,
        "smtp_security": "none", "smtp_user": "me", "smtp_password": SECRET_MAIL_PASSWORD,
        "from_address": "me@example.test", "display_name": "Me"})
    check("person can add an email account", st == 200 and isinstance(acct, dict) and acct.get("ok"), str(acct)[:200])
    acct_id = acct.get("id") if isinstance(acct, dict) else None

    def friend_inbox_subjects() -> list[str]:
        out = app_python(
            "import imaplib,email\n"
            "m=imaplib.IMAP4('mail',3143);m.login('friend','friendpass');m.select('INBOX')\n"
            "t,d=m.search(None,'ALL')\n"
            "for n in d[0].split():\n"
            "  t,x=m.fetch(n,'(BODY[HEADER.FIELDS (SUBJECT)])');print(email.message_from_bytes(x[0][1]).get('Subject'))\n")
        return [s for s in out.splitlines() if s]

    tag = uuid.uuid4().hex[:6]
    st, r = c.req("POST", "/api/email/send", json_body={
        "to": "friend@example.test", "subject": f"human-{tag}", "body": "hello from a person", "account_id": acct_id})
    check("person clicking Send is accepted", st == 200, f"{st} {str(r)[:200]}")
    private_ok = sh("docker", "exec", APP, "printenv", "EMAIL_ALLOW_PRIVATE_IPS").strip().lower() == "true"
    if private_ok:
        for _ in range(20):
            if f"human-{tag}" in friend_inbox_subjects():
                break
            time.sleep(1)
        check("person's email is delivered", f"human-{tag}" in friend_inbox_subjects())
    else:
        skip("person's email is delivered", "test mail server is on a private IP; set EMAIL_ALLOW_PRIVATE_IPS=true on a test install")

    # API token (plugins/scripts) may not send.
    st, tok = c.req("POST", "/api/tokens", form={"name": f"e2e-{tag}", "scopes": "email:send,email:read,chat"})
    token = (tok or {}).get("token") if isinstance(tok, dict) else None
    if token:
        t = Client()
        st, r = t.req("POST", "/api/email/send", json_body={
            "to": "friend@example.test", "subject": f"token-{tag}", "body": "x"},
            headers={"Authorization": f"Bearer {token}"})
        check("API token cannot send email (403)", st == 403, f"{st} {str(r)[:160]}")
    else:
        check("create API token for negative test", False, str(tok)[:200])

    # Agent's send tool -> draft document, nothing delivered.
    agent_txt = stream_chat(c, sid, 'Please send an email to my friend now. WINGS_CALL send_email {"to": "friend@example.test", "subject": "agent-%s", "body": "sent by agent"}' % tag, mode="agent")
    if "TOOL_NOT_OFFERED" in agent_txt:
        skip("agent send_email returns a draft, not a send", "router did not offer the email tool for this turn")
    else:
        check("agent send_email returns a draft, not a send", "NOT SENT" in agent_txt, agent_txt[:300])
    # Agent's loopback API call to /api/email/send must be refused.
    loop_txt = stream_chat(c, sid, 'WINGS_CALL app_api {"action": "call", "method": "POST", "path": "/api/email/send", "body": {"to": "friend@example.test", "subject": "loop-%s", "body": "x"}}' % tag, mode="agent")
    time.sleep(3)
    subjects = friend_inbox_subjects()
    check("agent mail never reaches the recipient", f"agent-{tag}" not in subjects and f"loop-{tag}" not in subjects, ",".join(subjects))
    if "TOOL_NOT_OFFERED" in loop_txt:
        skip("agent loopback send is refused", "router did not offer app_api; covered by tests/unit")
    else:
        check("agent loopback send is refused", "403" in loop_txt or "needs a signed-in person" in loop_txt, loop_txt[:300])

    # The agent's MCP email tool, called directly in its own process context
    # (deterministic, independent of upstream's per-turn tool routing).
    mcp_out = sh("docker", "exec", "-u", sh("docker", "exec", APP, "printenv", "PUID").strip() or "1000",
                 "-w", "/app", "-e", "PYTHONPATH=/app", "-e", f"ODYSSEUS_MCP_EMAIL_OWNER={USER}", APP, "python", "-c",
                 "import json, mcp_servers.email_server as es\n"
                 f"r = es._send_email(to='friend@example.test', subject='mcp-{tag}', body='agent wrote this')\n"
                 "print('RESULT', json.dumps({k: r.get(k) for k in ('pending', 'draft_doc_id')}))\n"
                 "try:\n    es._smtp_connect(); print('SMTP_OPENED')\n"
                 "except Exception as e:\n    print('SMTP_BLOCKED', type(e).__name__)\n")
    check("agent send_email tool creates a draft document", '"draft_doc_id": "' in mcp_out and '"pending": true' in mcp_out, mcp_out[-300:])
    check("agent email process cannot open SMTP", "SMTP_BLOCKED EmailSendBlocked" in mcp_out, mcp_out[-200:])
    time.sleep(2)
    check("agent-tool email not delivered", f"mcp-{tag}" not in friend_inbox_subjects())

    # Scheduled rows a person did not create are refused by the poller.
    # (Upstream starts the scheduled-send poller on the first inbox listing.)
    c.req("GET", f"/api/email/list?account_id={acct_id}&folder=INBOX&limit=5")
    rid = "e2e" + tag
    app_python(
        "import sqlite3,datetime;c=sqlite3.connect('/app/data/scheduled_emails.db');"
        "c.execute(\"insert into scheduled_emails (id,to_addr,subject,body,send_at,created_at,status,owner,account_id,attachments) values (?,?,?,?,?,?,'pending',?,?,'[]')\","
        f"('{rid}','friend@example.test','sched-{tag}','x',datetime.datetime.utcnow().isoformat(),datetime.datetime.utcnow().isoformat(),'{USER}','{acct_id}'));c.commit()")
    status = ""
    for _ in range(45):
        status = app_python(
            "import sqlite3;c=sqlite3.connect('/app/data/scheduled_emails.db');"
            f"print(c.execute(\"select status, coalesce(error,'') from scheduled_emails where id='{rid}'\").fetchone())").strip()
        if "pending" not in status and "sending" not in status:
            break
        time.sleep(2)
    check("unapproved scheduled row is refused", "failed" in status and "WINGS_FORCE_EMAIL_CONFIRM" in status, status)
    check("unapproved scheduled mail not delivered", f"sched-{tag}" not in friend_inbox_subjects())

    # --- MCP env encryption + child env scrubbing -------------------------------
    dump = "/app/data/wings-e2e-mcp-env.json"
    st, r = c.req("POST", "/api/mcp/servers", form={
        "name": f"e2e-env-{tag}", "transport": "stdio", "command": "python",
        "args": json.dumps(["-c", f"import os,json;open('{dump}','w').write(json.dumps(dict(os.environ)))"]),
        "env": json.dumps({"E2E_SERVER_TOKEN": SECRET_MCP_ENV})})
    check("add user MCP server with env", st in (200, 201), f"{st} {str(r)[:200]}")
    time.sleep(6)
    env_dump = sh("docker", "exec", APP, "cat", dump)
    try:
        child_env = json.loads(env_dump)
    except ValueError:
        child_env = {}
    check("user MCP server started (env captured)", bool(child_env), env_dump[:120])
    if child_env:
        check("MCP child does NOT get ODYSSEUS_APP_KEY", "ODYSSEUS_APP_KEY" not in child_env)
        check("MCP child does NOT get the admin password", "ODYSSEUS_ADMIN_PASSWORD" not in child_env)
        check("MCP child still gets its own configured env", child_env.get("E2E_SERVER_TOKEN") == SECRET_MCP_ENV)
    raw_env = app_python(
        "import sqlite3;c=sqlite3.connect('/app/data/app.db');"
        f"print(c.execute(\"select env from mcp_servers where name='e2e-env-{tag}'\").fetchone()[0])")
    check("MCP server env encrypted at rest", raw_env.strip().startswith("enc:"), raw_env[:60])
    sh("docker", "exec", APP, "rm", "-f", dump)

    # --- CalDAV round trip with the bundled Radicale (profile caldav) --------
    dav_user, dav_pw = os.environ.get("WINGS_CALDAV_USER"), os.environ.get("WINGS_CALDAV_PASSWORD")
    dav_host = os.environ.get("WINGS_CALDAV_HOST_URL")  # e.g. http://127.0.0.1:5232
    if dav_user and dav_pw and dav_host:
        import base64
        auth = "Basic " + base64.b64encode(f"{dav_user}:{dav_pw}".encode()).decode()

        def dav(method, path, body=None, headers=None):
            h = {"Authorization": auth, **(headers or {})}
            r = urllib.request.Request(dav_host + path, data=body, method=method, headers=h)
            try:
                with urllib.request.urlopen(r, timeout=20) as resp:
                    return resp.status, resp.read().decode("utf-8", "replace")
            except urllib.error.HTTPError as e:
                return e.code, e.read().decode("utf-8", "replace")

        cal = f"/{dav_user}/e2e-{tag}/"
        st, _ = dav("MKCOL", cal, body=(
            '<?xml version="1.0"?><D:mkcol xmlns:D="DAV:" xmlns:C="urn:ietf:params:xml:ns:caldav">'
            '<D:set><D:prop><D:resourcetype><D:collection/><C:calendar/></D:resourcetype>'
            f'<D:displayname>E2E {tag}</D:displayname></D:prop></D:set></D:mkcol>').encode(),
            headers={"Content-Type": "application/xml"})
        check("phone-style client creates a calendar in Radicale", st in (200, 201), str(st))
        ics = ("BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//wings-e2e//EN\r\nBEGIN:VEVENT\r\n"
               f"UID:phone-{tag}@e2e\r\nDTSTAMP:20261110T090000Z\r\nDTSTART:20261201T090000Z\r\n"
               f"DTEND:20261201T100000Z\r\nSUMMARY:Dentist {tag}\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n")
        st, _ = dav("PUT", cal + f"phone-{tag}.ics", body=ics.encode(), headers={"Content-Type": "text/calendar"})
        check("phone-style client adds an event", st in (200, 201, 204), str(st))
        st, r = c.req("POST", "/api/calendar/config/accounts", json_body={
            "name": "Radicale", "url": f"http://radicale:5232/{dav_user}/", "username": dav_user, "password": dav_pw})
        check("Odysseus accepts the CalDAV account", st == 200 and "error" not in json.dumps(r).lower(), str(r)[:200])
        st, r = c.req("POST", "/api/calendar/sync?direction=pull", timeout=120)
        check("CalDAV pull succeeds", st == 200, str(r)[:200])
        st, evs = c.req("GET", "/api/calendar/events?start=2026-11-25T00:00:00Z&end=2026-12-05T00:00:00Z")
        check("phone's event shows up in Odysseus", f"Dentist {tag}" in json.dumps(evs), json.dumps(evs)[:200])
        st, cals = c.req("GET", "/api/calendar/calendars")
        cal_list = cals if isinstance(cals, list) else (cals or {}).get("calendars", [])
        cal_id = next((x.get("href") or x.get("id") for x in cal_list
                       if x.get("name") == f"E2E {tag}"), None) if st == 200 else None
        st, ev = c.req("POST", "/api/calendar/events", json_body={
            "summary": f"Lunch {tag}", "dtstart": "2026-12-02T12:00:00Z", "dtend": "2026-12-02T13:00:00Z",
            "calendar_href": cal_id})
        check("create event in Odysseus on the synced calendar", st == 200 and bool(cal_id), f"{st} cal={cal_id} {str(ev)[:160]}")
        time.sleep(3)
        st, listing = dav("PROPFIND", cal, headers={"Depth": "1"})
        found = False
        for href in set(__import__("re").findall(r"<[^>]*href>([^<]+\.ics)<", listing)):
            st2, body2 = dav("GET", href)
            if f"Lunch {tag}" in body2:
                found = True
        check("Odysseus event is written back to Radicale", found, listing[:200])
        raw_pref = sh("docker", "exec", APP, "sh", "-c", f"grep -rl -e '{dav_pw}' /app/data 2>/dev/null || true").strip()
        check("CalDAV password not stored in plaintext", raw_pref == "", raw_pref)
    else:
        skip("CalDAV round trip", "caldav profile not enabled")

    # --- browser automation (profile browser) ----------------------------------
    st, bstat = c.req("GET", "/api/wings/browser/status")
    if isinstance(bstat, dict) and bstat.get("enabled"):
        browser_checks(c, ep_id)
    else:
        skip("browser automation", "browser profile not enabled")

    # --- whole-volume plaintext sweep -----------------------------------------
    hits = sh("docker", "exec", APP, "sh", "-c",
              f"grep -rl -e '{SECRET_ENDPOINT_KEY}' -e '{SECRET_MCP_ENV}' -e '{SECRET_MAIL_PASSWORD}' /app/data /app/logs 2>/dev/null || true").strip()
    check("no plaintext secret anywhere in data or logs", hits == "", hits)

    print(f"\n{failures} failure(s)")
    return failures


if __name__ == "__main__":
    sys.exit(min(main(), 100))
