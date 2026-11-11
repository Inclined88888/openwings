#!/usr/bin/env python3
"""Gate in front of Chromium DevTools and the VNC WebSocket bridge.

Chromium's DevTools protocol and the VNC bridge have no authentication of
their own, and whoever reaches them controls the logged-in browser. DevTools
listens on loopback inside this container and the VNC bridge on a unix
socket. This proxy is the only way in from outside, and every connection must
pass two checks:

  * its source address belongs to a container named in WINGS_BROWSER_CLIENTS
    (default: the Wings app, "odysseus"), as resolved by Docker's DNS on each
    connection, so other containers and the Docker host are refused;
  * its HTTP request (the DevTools JSON/WebSocket handshake or the VNC
    WebSocket handshake) carries X-Wings-Browser-Token equal to
    WINGS_BROWSER_TOKEN. The header is removed before forwarding. With no
    token configured, everything is refused.

  WINGS_CDP_PORT  (9223) -> 127.0.0.1:9222            Chromium DevTools
  WINGS_VIEW_PORT (7900) -> /tmp/wings/novnc.sock      noVNC WebSocket bridge
"""
import asyncio
import hmac
import os
import socket
import sys
import time

CLIENTS = [c.strip() for c in os.environ.get("WINGS_BROWSER_CLIENTS", "odysseus").split(",") if c.strip()]
TOKEN = os.environ.pop("WINGS_BROWSER_TOKEN", "").strip()
HEADER = b"x-wings-browser-token"
ROUTES = [
    (int(os.environ.get("WINGS_CDP_PORT", "9223")), ("tcp", "127.0.0.1", 9222)),
    (int(os.environ.get("WINGS_VIEW_PORT", "7900")), ("unix", "/tmp/wings/novnc.sock", None)),
]
_warned = {}


def _warn(key, message):
    now = time.monotonic()
    if now - _warned.get(key, 0) > 60:
        _warned[key] = now
        print(f"guard: {message}", file=sys.stderr, flush=True)


async def _peer_allowed(peer_ip):
    """Resolve the allowed names on every connection (no cache), so an address
    freed by a stopped container can never be reused by another one."""
    if peer_ip.startswith("::ffff:"):
        peer_ip = peer_ip[7:]
    for name in CLIENTS:
        try:
            for info in await asyncio.get_running_loop().getaddrinfo(name, None, proto=socket.IPPROTO_TCP):
                if info[4][0] == peer_ip:
                    return True
        except OSError:
            pass
    return False


async def _read_head(reader):
    """The HTTP request head (request line and headers), at most 16 KB."""
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = await asyncio.wait_for(reader.read(4096), 10)
        if not chunk:
            return None, b""
        data += chunk
        if len(data) > 16384:
            return None, b""
    head, _, rest = data.partition(b"\r\n\r\n")
    return head, rest


def _check_and_strip(head):
    lines = head.split(b"\r\n")
    kept, ok = [lines[0]], False
    for line in lines[1:]:
        name, _, value = line.partition(b":")
        if name.strip().lower() == HEADER:
            ok = ok or hmac.compare_digest(value.strip(), TOKEN.encode())
            continue
        kept.append(line)
    return ok, b"\r\n".join(kept) + b"\r\n\r\n"


async def _pipe(reader, writer):
    try:
        while True:
            data = await reader.read(65536)
            if not data:
                break
            writer.write(data)
            await writer.drain()
    except (ConnectionError, asyncio.CancelledError):
        pass
    finally:
        try:
            writer.close()
        except Exception:
            pass


def _handler(target):
    kind, host, port = target

    async def handle(reader, writer):
        peer = writer.get_extra_info("peername") or ("?", 0)
        if not TOKEN:
            _warn("token", "WINGS_BROWSER_TOKEN is not set; refusing every connection. Run ./scripts/setup.sh.")
            writer.close()
            return
        if not await _peer_allowed(str(peer[0])):
            _warn(peer[0], f"refused connection from {peer[0]} (not {', '.join(CLIENTS)})")
            writer.close()
            return
        try:
            head, rest = await _read_head(reader)
        except (asyncio.TimeoutError, ConnectionError):
            head = None
        if head is None:
            writer.close()
            return
        ok, head = _check_and_strip(head)
        if not ok:
            _warn("badtoken" + str(peer[0]), f"refused request from {peer[0]}: missing or wrong token")
            try:
                writer.write(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
                await writer.drain()
            except ConnectionError:
                pass
            writer.close()
            return
        try:
            if kind == "unix":
                up_reader, up_writer = await asyncio.open_unix_connection(host)
            else:
                up_reader, up_writer = await asyncio.open_connection(host, port)
        except OSError:
            writer.close()
            return
        up_writer.write(head + rest)
        await up_writer.drain()
        await asyncio.gather(_pipe(reader, up_writer), _pipe(up_reader, writer))
    return handle


async def main():
    servers = []
    for listen_port, target in ROUTES:
        servers.append(await asyncio.start_server(_handler(target), host="0.0.0.0", port=listen_port))
    state = "token set" if TOKEN else "NO TOKEN: refusing everything"
    print(f"guard: admitting {', '.join(CLIENTS)} on ports {', '.join(str(p) for p, _ in ROUTES)} ({state})", flush=True)
    await asyncio.gather(*(s.serve_forever() for s in servers))


if __name__ == "__main__":
    asyncio.run(main())
