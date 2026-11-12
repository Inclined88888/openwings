#!/usr/bin/env python3
"""Deterministic OpenAI-compatible model server for OpenWings end-to-end tests.

Standard library only. Serves /v1/models and /v1/chat/completions (streaming
and non-streaming, including tool calls) so tests can drive Odysseus' real
chat and agent paths without a GPU or a paid API.

Behaviour, keyed off the LAST message in the request:
  * user text containing  WINGS_CALL <tool_name> <json-args>
      -> if <tool_name> (or a tool ending in __<tool_name>) is among the
         offered tools, reply with that tool call; else reply
         "TOOL_NOT_OFFERED <tool_name>".
  * user text containing  WINGS_LIST_TOOLS
      -> reply "TOOLS: a,b,c" with the names of all offered tools.
  * a tool result message
      -> reply "TOOL_RESULT: <first 300 chars of the result>".
  * anything else
      -> reply "[<model>] <first 200 chars of the user text>".
  * model "mock-ratelimited" (list it in MOCK_MODELS)
      -> HTTP 429 "try again in 0.4s" twice per conversation, then as above.

Every request is appended to $MOCK_LOG (default /tmp/mock_llm.jsonl) as
{"model", "stream", "tools": [names], "last_role", "last_text"} so tests can
assert exactly which tools a turn was offered.
"""
from __future__ import annotations

import json
import threading
import os
import re
import sys
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MODELS = [m for m in os.environ.get("MOCK_MODELS", "mock-alpha,mock-beta").split(",") if m]
LOG = os.environ.get("MOCK_LOG", "/tmp/mock_llm.jsonl")
_RATE_SEEN: dict = {}
_RATE_LOCK = threading.Lock()
CALL_RE = re.compile(r"WINGS_CALL\s+([A-Za-z0-9_.:-]+)\s*(\{.*\})?", re.S)


def _text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(p.get("text", "") for p in content if isinstance(p, dict))
    return ""


def _tool_names(tools) -> list[str]:
    names = []
    for t in tools or []:
        fn = (t or {}).get("function") or {}
        name = fn.get("name") or (t or {}).get("name")
        if name:
            names.append(name)
    return names


def _decide(body: dict):
    """Return ("text", str) or ("tool", name, args_json)."""
    model = body.get("model", "mock")
    msgs = body.get("messages") or []
    offered = _tool_names(body.get("tools"))
    last = msgs[-1] if msgs else {}
    role = last.get("role", "")
    text = _text(last.get("content"))
    if role == "tool":
        return ("text", "TOOL_RESULT: " + text[:300].replace("\n", " "))
    # A result already answered the latest WINGS_CALL: the app may add
    # context after it (a tool result whose text came from outside, such as
    # a mail or a file, is passed back as a marked untrusted block).
    calls = [i for i, m in enumerate(msgs) if m.get("role") == "user" and CALL_RE.search(_text(m.get("content")))]
    if calls:
        after = msgs[calls[-1] + 1:]
        results = [m for m in after if m.get("role") == "tool"
                   or (m.get("role") == "user" and "untrusted" in _text(m.get("content")).lower())]
        if results:
            return ("text", "TOOL_RESULT: " + _text(results[-1].get("content"))[:300].replace("\n", " "))
    # Agent loops often re-send the conversation with the user turn earlier.
    if role != "user":
        for m in reversed(msgs):
            if m.get("role") == "user":
                text = _text(m.get("content"))
                break
    if "WINGS_LIST_TOOLS" in text:
        return ("text", "TOOLS: " + ",".join(sorted(offered)))
    m = CALL_RE.search(text)
    if m:
        wanted, args = m.group(1), (m.group(2) or "{}")
        match = next((n for n in offered if n == wanted or n.endswith("__" + wanted)), None)
        if not match:
            return ("text", "TOOL_NOT_OFFERED " + wanted)
        try:
            json.loads(args)
        except ValueError:
            return ("text", "BAD_ARGS " + wanted)
        return ("tool", match, args)
    return ("text", f"[{model}] " + text[:200].replace("\n", " "))


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # quiet
        sys.stderr.write(f"mock_llm {time.strftime('%H:%M:%S')}: " + (fmt % args) + "\n")

    def setup(self):
        super().setup()
        if os.environ.get("MOCK_DEBUG"):
            sys.stderr.write(f"mock_llm {time.strftime('%H:%M:%S')}: connect from {self.client_address}\n")

    def _json(self, code: int, obj) -> None:
        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path.rstrip("/").endswith("/models"):
            return self._json(200, {"object": "list", "data": [
                {"id": m, "object": "model", "created": 0, "owned_by": "wings-mock"} for m in MODELS]})
        if self.path in ("/", "/health"):
            return self._json(200, {"ok": True})
        return self._json(404, {"error": "not found"})

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            return self._json(400, {"error": "bad json"})
        if not self.path.rstrip("/").endswith("/chat/completions"):
            return self._json(404, {"error": "not found"})
        # Rate limit drill: this model answers 429 twice per conversation
        # text, then normally (checks the app's retry with backoff).
        if body.get("model") == "mock-ratelimited":
            key = json.dumps(body.get("messages") or [])[-400:]
            with _RATE_LOCK:
                _RATE_SEEN[key] = _RATE_SEEN.get(key, 0) + 1
                count = _RATE_SEEN[key]
            if count <= 2:
                return self._json(429, {"error": {"message": "Rate limit reached. Please try again in 0.4s.",
                                                  "type": "rate_limit_error"}})
        decision = _decide(body)
        msgs = body.get("messages") or []
        try:
            with open(LOG, "a", encoding="utf-8") as fh:
                fh.write(json.dumps({
                    "model": body.get("model"), "stream": bool(body.get("stream")),
                    "tools": _tool_names(body.get("tools")),
                    "last_role": (msgs[-1].get("role") if msgs else None),
                    "last_text": _text(msgs[-1].get("content"))[:500] if msgs else "",
                    "decision": decision[0],
                }) + "\n")
        except OSError:
            pass
        cid = "chatcmpl-" + uuid.uuid4().hex[:12]
        model = body.get("model", "mock")
        created = int(time.time())
        if not body.get("stream"):
            if decision[0] == "tool":
                msg = {"role": "assistant", "content": None, "tool_calls": [{
                    "id": "call_" + uuid.uuid4().hex[:8], "type": "function",
                    "function": {"name": decision[1], "arguments": decision[2]}}]}
                finish = "tool_calls"
            else:
                msg = {"role": "assistant", "content": decision[1]}
                finish = "stop"
            return self._json(200, {"id": cid, "object": "chat.completion", "created": created,
                                    "model": model, "choices": [{"index": 0, "message": msg, "finish_reason": finish}],
                                    "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}})
        # Stream with chunked transfer encoding, as real OpenAI-compatible
        # servers do, so clients can tell where the response ends and return
        # the connection to their pool without waiting for a close.
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

        def write_chunk(data: bytes) -> None:
            self.wfile.write(f"{len(data):x}\r\n".encode() + data + b"\r\n")
            self.wfile.flush()

        def chunk(delta, finish=None):
            payload = {"id": cid, "object": "chat.completion.chunk", "created": created, "model": model,
                       "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
            write_chunk(b"data: " + json.dumps(payload).encode() + b"\n\n")

        chunk({"role": "assistant"})
        if decision[0] == "tool":
            chunk({"tool_calls": [{"index": 0, "id": "call_" + uuid.uuid4().hex[:8], "type": "function",
                                   "function": {"name": decision[1], "arguments": ""}}]})
            chunk({"tool_calls": [{"index": 0, "function": {"arguments": decision[2]}}]})
            chunk({}, "tool_calls")
        else:
            words = decision[1].split(" ")
            for i, w in enumerate(words):
                chunk({"content": (w if i == 0 else " " + w)})
                time.sleep(0.01)
            chunk({}, "stop")
        write_chunk(b"data: [DONE]\n\n")
        self.wfile.write(b"0\r\n\r\n")
        self.wfile.flush()


class Server(ThreadingHTTPServer):
    # socketserver's default listen backlog is 5; the app opens bursts of
    # parallel connections (chat stream, title, memory, probes), and dropped
    # SYNs look like a dead model server. Real servers use hundreds.
    request_queue_size = 256
    daemon_threads = True


def main() -> None:
    port = int(os.environ.get("MOCK_PORT", "9999"))
    Server(("0.0.0.0", port), Handler).serve_forever()


if __name__ == "__main__":
    main()
