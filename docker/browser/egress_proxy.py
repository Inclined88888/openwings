#!/usr/bin/env python3
"""Forward proxy for Chromium that refuses private and local destinations.

Chromium is started with --proxy-server pointing here and with no bypass for
loopback, so every page load, redirect, iframe, script, image, form post and
WebSocket goes through this proxy. The proxy resolves the host itself and
connects to the address it checked, so DNS tricks (a name that resolves to a
public address for the check and a private one for the connection) do not
work. Refused: loopback, private, link-local (cloud metadata), CGNAT and
other non-global addresses, including IPv4 embedded in IPv6 (mapped, 6to4,
NAT64). WINGS_BROWSER_ALLOW_PRIVATE=1 turns the refusal off.

Listens on 127.0.0.1:3128 inside the browser container only.
"""
import asyncio
import ipaddress
import os
import socket
import sys
import time
from urllib.parse import urlsplit

ALLOW_PRIVATE = os.environ.get("WINGS_BROWSER_ALLOW_PRIVATE", "").strip().lower() in ("1", "true", "yes", "on")
LISTEN = ("127.0.0.1", 3128)
_NAT64 = ipaddress.ip_network("64:ff9b::/96")
_warned = {}


def ip_is_public(text):
    try:
        addr = ipaddress.ip_address(text.split("%", 1)[0])
    except ValueError:
        return False
    if addr.version == 6:
        if addr.ipv4_mapped is not None:
            addr = addr.ipv4_mapped
        elif addr.sixtofour is not None:
            addr = addr.sixtofour
        elif addr in _NAT64:
            addr = ipaddress.ip_address(int(addr) & 0xFFFFFFFF)
    return bool(addr.is_global) and not addr.is_multicast


def _warn(host, why):
    now = time.monotonic()
    if now - _warned.get(host, 0) > 30:
        _warned[host] = now
        print(f"egress: refused {host}: {why}", file=sys.stderr, flush=True)


async def _resolve(host, port):
    """The address to connect to, or raise PermissionError/OSError."""
    host = host.strip("[]")
    try:
        literal = ipaddress.ip_address(host)
        candidates = [str(literal)]
    except ValueError:
        infos = await asyncio.get_running_loop().getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
        candidates = [info[4][0] for info in infos]
    if not candidates:
        raise OSError("no address")
    if not ALLOW_PRIVATE:
        bad = [a for a in candidates if not ip_is_public(a)]
        if bad:
            raise PermissionError(f"{host} is a private or local address ({bad[0]})")
    return candidates[0]


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


async def _reply(writer, status, text):
    body = text.encode()
    try:
        writer.write(b"HTTP/1.1 " + status.encode() + b"\r\nContent-Type: text/plain\r\nContent-Length: "
                     + str(len(body)).encode() + b"\r\nConnection: close\r\n\r\n" + body)
        await writer.drain()
    except ConnectionError:
        pass
    writer.close()


async def handle(reader, writer):
    try:
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = await asyncio.wait_for(reader.read(8192), 30)
            if not chunk:
                writer.close()
                return
            data += chunk
            if len(data) > 65536:
                return await _reply(writer, "431 Request Header Fields Too Large", "headers too large")
    except (asyncio.TimeoutError, ConnectionError):
        writer.close()
        return
    head, _, rest = data.partition(b"\r\n\r\n")
    lines = head.split(b"\r\n")
    try:
        method, target, version = lines[0].decode("latin-1").split(" ", 2)
    except ValueError:
        return await _reply(writer, "400 Bad Request", "bad request line")
    try:
        if method.upper() == "CONNECT":
            host, _, port = target.rpartition(":")
            port = int(port)
            address = await _resolve(host, port)
            up_reader, up_writer = await asyncio.open_connection(address, port)
            writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            await writer.drain()
            if rest:
                up_writer.write(rest)
            await asyncio.gather(_pipe(reader, up_writer), _pipe(up_reader, writer))
            return
        parts = urlsplit(target)
        if parts.scheme.lower() not in ("http", "ws") or not parts.hostname:
            return await _reply(writer, "400 Bad Request", "absolute http URL required")
        port = parts.port or 80
        address = await _resolve(parts.hostname, port)
        path = parts.path or "/"
        if parts.query:
            path += "?" + parts.query
        headers = [line for line in lines[1:]
                   if line.split(b":", 1)[0].strip().lower() not in (b"proxy-connection", b"proxy-authorization",
                                                                      b"connection", b"keep-alive")]
        # One request per connection, so a reused client connection can never
        # carry a request for a different, unchecked host.
        new_head = b"\r\n".join([f"{method} {path} {version}".encode("latin-1"), *headers, b"Connection: close"])
        up_reader, up_writer = await asyncio.open_connection(address, port)
        up_writer.write(new_head + b"\r\n\r\n" + rest)
        await up_writer.drain()
        await asyncio.gather(_pipe(reader, up_writer), _pipe(up_reader, writer))
    except PermissionError as exc:
        _warn(target, str(exc))
        await _reply(writer, "403 Forbidden", f"OpenWings: {exc}. Set WINGS_BROWSER_ALLOW_PRIVATE=1 to allow it.")
    except (OSError, ValueError) as exc:
        await _reply(writer, "502 Bad Gateway", f"OpenWings: could not connect ({exc.__class__.__name__})")


async def main():
    server = await asyncio.start_server(handle, host=LISTEN[0], port=LISTEN[1])
    print(f"egress: listening on {LISTEN[0]}:{LISTEN[1]}; private addresses {'ALLOWED' if ALLOW_PRIVATE else 'refused'}",
          flush=True)
    await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
