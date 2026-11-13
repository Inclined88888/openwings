"""Sign in off (patch 0029): wings/local_guard.py and the entrypoint switch."""
import asyncio
import ipaddress
import os
import re
import subprocess
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "..", "overlay", "app"))
sys.path.insert(0, "/app")

from wings import local_guard as G  # noqa: E402

NETS = [ipaddress.ip_network("172.30.57.0/24"), ipaddress.ip_network("172.31.0.0/16")]
GW = "172.30.57.1"


def scope(method="GET", path="/", host="localhost:7011", client=GW, kind="http", scheme="http", extra=(), **hdrs):
    headers = [(b"host", host.encode())] if host is not None else []
    for k, v in hdrs.items():
        headers.append((k.replace("_", "-").encode(), v.encode()))
    headers.extend(extra)
    return {"type": kind, "method": method, "path": path, "scheme": scheme,
            "client": (client, 50000) if client else None, "headers": headers}


def nav(**kw):
    kw.setdefault("sec_fetch_mode", "navigate")
    return scope(**kw)


class Refusal(unittest.TestCase):
    def r(self, sc=None, **kw):
        return G.refusal(sc or scope(**kw), NETS)

    def test_own_page_passes(self):
        self.assertIsNone(self.r())
        self.assertIsNone(self.r(method="POST", path="/api/chat", origin="http://localhost:7011", sec_fetch_site="same-origin"))
        self.assertIsNone(self.r(host="127.0.0.1:7011", method="POST", path="/api/x", origin="http://127.0.0.1:7011"))
        self.assertIsNone(self.r(host="[::1]:7011", method="POST", path="/api/x", origin="http://[::1]:7011"))
        self.assertIsNone(self.r(kind="websocket", scheme="ws", path="/ws", origin="http://localhost:7011"))

    def test_app_calling_itself_passes(self):
        self.assertIsNone(self.r(client="127.0.0.1", host="127.0.0.1:7011", method="POST", path="/api/x"))
        self.assertIsNone(self.r(client="::1", host="localhost:7000", method="POST", path="/api/x"))

    def test_other_website_refused(self):
        self.assertEqual(self.r(method="POST", path="/api/session", origin="https://evil.example"), "origin")
        self.assertEqual(self.r(method="POST", path="/api/session", origin="null"), "origin")
        self.assertEqual(self.r(method="POST", path="/api/session", origin="http://localhost:3000"), "origin")
        self.assertEqual(self.r(method="POST", path="/api/session", sec_fetch_site="cross-site"), "site")
        self.assertEqual(self.r(method="POST", path="/api/session", sec_fetch_site="same-site"), "site")
        self.assertEqual(self.r(path="/api/sessions", sec_fetch_site="cross-site"), "site")
        self.assertEqual(self.r(path="/api/sessions", sec_fetch_site="cross-site", sec_fetch_mode="cors"), "site")
        self.assertEqual(self.r(kind="websocket", scheme="ws", path="/ws", origin="https://evil.example"), "origin")
        self.assertEqual(self.r(kind="websocket", scheme="ws", path="/ws", sec_fetch_site="cross-site"), "site")
        two = [(b"origin", b"http://localhost:7011"), (b"origin", b"https://evil.example")]
        self.assertEqual(self.r(method="POST", path="/api/x", extra=two), "origin")

    def test_page_loads_from_other_sites_pass(self):
        # A provider sending the browser back after a sign in, or a link.
        self.assertIsNone(self.r(nav(path="/", sec_fetch_site="cross-site")))
        self.assertIsNone(self.r(nav(path="/api/wings/connect/openrouter/callback", sec_fetch_site="cross-site")))
        self.assertIsNone(self.r(nav(path="/api/email/oauth/google/callback", sec_fetch_site="cross-site")))
        self.assertEqual(self.r(nav(method="POST", path="/api/x", sec_fetch_site="cross-site")), "site")

    def test_dns_rebinding_refused(self):
        self.assertEqual(self.r(host="evil.example:7011"), "host")
        self.assertEqual(self.r(host="openwings:7000"), "host")
        self.assertEqual(self.r(host=None), "host")
        self.assertEqual(self.r(host="localhost@evil.example"), "host")
        self.assertEqual(self.r(extra=[(b"host", b"evil.example")]), "host")

    def test_only_loopback_and_gateways_are_clients(self):
        self.assertEqual(self.r(client="172.30.57.5", host="localhost:7000"), "client")
        self.assertEqual(self.r(client="172.31.0.3", host="localhost:7000"), "client")
        self.assertIsNone(self.r(client="172.31.0.1"))
        # Unknown addresses (a network attached later, a routed LAN host).
        self.assertEqual(self.r(client="10.9.0.5"), "client")
        self.assertEqual(self.r(client="192.168.1.50"), "client")
        self.assertEqual(self.r(client=None), "client")
        self.assertEqual(self.r(client="fd00::5"), "client")
        self.assertEqual(self.r(client="not-an-ip"), "client")

    def test_proxy_refused(self):
        self.assertEqual(self.r(x_forwarded_for="1.2.3.4"), "proxied")
        self.assertEqual(self.r(forwarded="for=1.2.3.4"), "proxied")

    def test_lifespan_untouched(self):
        self.assertIsNone(G.refusal({"type": "lifespan"}, NETS))

    def test_unreadable_route_table_fails_closed(self):
        self.assertEqual(G._route_file_networks("/nonexistent/route"), [])
        self.assertFalse(G.client_allowed(GW, []))


class Routes(unittest.TestCase):
    def test_parses_proc_route(self):
        import tempfile
        text = ("Iface\tDestination\tGateway \tFlags\tRefCnt\tUse\tMetric\tMask\t\tMTU\tWindow\tIRTT\n"
                "eth0\t00000000\t01391EAC\t0003\t0\t0\t0\t00000000\t0\t0\t0\n"
                "eth0\t00391EAC\t00000000\t0001\t0\t0\t0\t00FFFFFF\t0\t0\t0\n"
                "eth1\t0000FFAC\t00000000\t0001\t0\t0\t0\t0000FFFF\t0\t0\t0\n")
        with tempfile.NamedTemporaryFile("w", delete=False) as fh:
            fh.write(text)
        try:
            nets = G._route_file_networks(fh.name)
        finally:
            os.unlink(fh.name)
        self.assertEqual([str(n) for n in nets], ["172.30.57.0/24", "172.255.0.0/16"])


class FakeAuth:
    is_configured = True

    def __init__(self):
        self.users = {"admin": {"is_admin": True}, "bob": {}}
        self.valid = {"good"}
        self.made = []

    def is_admin(self, u):
        return self.users.get(u, {}).get("is_admin", False)

    def validate_token(self, t):
        return t in self.valid

    def create_session_trusted(self, u):
        self.made.append(u)
        return "fresh"


class Middleware(unittest.TestCase):
    def setUp(self):
        G._cache.update(at=float("inf"), nets=NETS)
        try:
            import routes.auth_routes  # noqa: F401
            import core.auth  # noqa: F401
        except Exception:
            self.skipTest("needs the app (runs in the image)")

    def tearDown(self):
        G._cache.update(at=0.0, nets=[])

    def run_app(self, sc, auth=None):
        sent, seen = [], []

        async def inner(scope, receive, send):
            seen.append(scope)
            await send({"type": "http.response.start", "status": 200, "headers": []})

        async def send(msg):
            sent.append(msg)

        async def receive():
            return {"type": "http.request"}

        class App:
            pass
        app = App()
        app.state = App()
        app.state.auth_manager = auth or FakeAuth()
        sc["app"] = app
        asyncio.run(G.LocalOnlyGuard(inner)(sc, receive, send))
        return sent, seen

    def cookie(self, sc):
        return dict(sc["headers"]).get(b"cookie", b"").decode()

    def test_refused_is_403_and_app_not_called(self):
        sent, seen = self.run_app(scope(method="POST", path="/api/chat", origin="https://evil.example"))
        self.assertFalse(seen)
        self.assertEqual(sent[0]["status"], 403)

    def test_websocket_closed(self):
        sent, seen = self.run_app(scope(kind="websocket", origin="https://evil.example"))
        self.assertFalse(seen)
        self.assertEqual(sent[0]["type"], "websocket.close")

    def test_page_load_signs_in_as_admin(self):
        auth = FakeAuth()
        sent, seen = self.run_app(nav(cookie="theme=x"), auth)
        self.assertEqual(auth.made, ["admin"])
        self.assertIn("openwings_session=fresh", self.cookie(seen[0]))
        self.assertIn("theme=x", self.cookie(seen[0]))
        sc = dict(sent[0]["headers"]).get(b"set-cookie", b"").decode()
        self.assertTrue(sc.startswith("openwings_session=fresh;") and "HttpOnly" in sc and "SameSite=Lax" in sc)

    def test_valid_session_kept(self):
        auth = FakeAuth()
        sent, seen = self.run_app(nav(cookie="openwings_session=good"), auth)
        self.assertEqual(auth.made, [])
        self.assertEqual(self.cookie(seen[0]), "openwings_session=good")

    def test_stale_session_replaced(self):
        auth = FakeAuth()
        sent, seen = self.run_app(nav(cookie="openwings_session=old"), auth)
        self.assertEqual(auth.made, ["admin"])
        self.assertEqual(self.cookie(seen[0]), "openwings_session=fresh")

    def test_no_session_for_scripts_the_app_itself_or_unconfigured(self):
        auth = FakeAuth()
        self.run_app(scope(path="/api/sessions"), auth)            # fetch, not a page load
        self.run_app(scope(method="POST", path="/api/x"), auth)
        self.run_app(nav(client="127.0.0.1", host="127.0.0.1:7000"), auth)  # the app itself
        self.assertEqual(auth.made, [])
        auth.is_configured = False
        self.run_app(nav(), auth)
        self.assertEqual(auth.made, [])

    def test_admin_choice(self):
        auth = FakeAuth()
        auth.users = {"zed": {"is_admin": True}, "amy": {"is_admin": True}}
        self.assertEqual(G._admin(auth), "amy")
        auth.users = {"bob": {}}
        self.assertIsNone(G._admin(auth))


class Entrypoint(unittest.TestCase):
    def snippet(self):
        for base in (os.path.join(HERE, "..", "..", "overlay", "app"), "/app"):
            p = os.path.join(base, "wings", "entrypoint.sh")
            if os.path.exists(p):
                text = open(p).read()
                return re.search(r'^case "\$\(printf.*?^esac$', text, re.S | re.M).group(0)
        self.skipTest("entrypoint.sh not found")

    def run_sh(self, **env):
        snip = self.snippet() + '\necho "OFF=${WINGS_SIGN_IN_OFF:-}"'
        full = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), **env}
        out = subprocess.run(["sh", "-c", snip], env=full, capture_output=True, text=True)
        return out.stdout.strip().splitlines()[-1]

    def test_switch(self):
        self.assertEqual(self.run_sh(), "OFF=")
        self.assertEqual(self.run_sh(OPENWINGS_SIGN_IN="on", OPENWINGS_APP_BIND="127.0.0.1"), "OFF=")
        self.assertEqual(self.run_sh(OPENWINGS_SIGN_IN="off", OPENWINGS_APP_BIND="127.0.0.1"), "OFF=1")
        self.assertEqual(self.run_sh(OPENWINGS_SIGN_IN="OFF", OPENWINGS_APP_BIND="localhost"), "OFF=1")
        self.assertEqual(self.run_sh(OPENWINGS_SIGN_IN="off", OPENWINGS_APP_BIND="0.0.0.0"), "OFF=")
        self.assertEqual(self.run_sh(OPENWINGS_SIGN_IN="off"), "OFF=")
        self.assertEqual(self.run_sh(OPENWINGS_SIGN_IN="off", OPENWINGS_APP_BIND="127.0.0.1",
                                     OPENWINGS_PROFILES="browser, https"), "OFF=")
        self.assertEqual(self.run_sh(OPENWINGS_SIGN_IN="off", OPENWINGS_APP_BIND="127.0.0.1",
                                     OPENWINGS_PROFILES="browser,runner"), "OFF=1")


if __name__ == "__main__":
    unittest.main()
