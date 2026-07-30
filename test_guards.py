#!/usr/bin/env python3
"""Tests for the local server's access guards.

`POST /api/sync` shells out to sync.py, so it is the only thing in this project
that changes state from a request. It is never deployed — the published site is
static files with no server — so the internet cannot reach it at all. What these
cover is the machine it *does* run on:

  * binding to loopback, so nothing on the network can connect;
  * a Host check, so DNS rebinding cannot dress up a local connection as a
    request to somebody else's domain;
  * an Origin check, so a page you happen to be browsing cannot fire a sync
    behind your back. Browsers send cross-origin POSTs even when the reply is
    unreadable, so "they can't see the response" is not a defence.

These drive a real server over a real socket, because the guards live in the
request handler and a unit test of the store would not touch them.

    python3 test_guards.py
"""

import socket
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

import serve

HERE = Path(__file__).resolve().parent
DB = HERE / "theatres.sqlite3"


class GuardCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not DB.is_file():
            raise unittest.SkipTest("no database; run ./sync.py first")

        serve.Handler.store = serve.VenueStore(DB)
        serve.Handler.db_path = DB
        serve.Handler.log_message = lambda *a, **k: None
        # Port 0 lets the kernel choose and the server hold it in one step.
        # Probing for a free port and then binding it leaves a window in which
        # another process can take it - which is exactly what happens when
        # three of these classes start in quick succession.
        cls.httpd = serve.ThreadingHTTPServer(("127.0.0.1", 0), serve.Handler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.port}"

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def request(self, path="/api/meta", method="GET", headers=None):
        req = urllib.request.Request(self.base + path, method=method,
                                     headers=headers or {})
        if method == "POST":
            req.data = b""
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                return resp.status
        except urllib.error.HTTPError as exc:
            return exc.code


class TestBinding(GuardCase):
    def test_the_server_is_not_listening_on_any_public_interface(self):
        """The first line of defence: nothing off-machine can even connect."""
        addrs = {a[4][0] for a in socket.getaddrinfo(socket.gethostname(), None)
                 if not a[4][0].startswith("127.")}
        for addr in list(addrs)[:3]:
            with self.subTest(address=addr):
                s = socket.socket(socket.AF_INET6 if ":" in addr else socket.AF_INET)
                s.settimeout(2)
                try:
                    refused = s.connect_ex((addr, self.port)) != 0
                finally:
                    s.close()
                self.assertTrue(refused, f"server accepted a connection on {addr}")

    def test_a_normal_local_request_still_works(self):
        self.assertEqual(self.request("/api/meta"), 200)


class TestHostHeader(GuardCase):
    def test_a_rebound_hostname_is_refused(self):
        # DNS rebinding: the connection really is from 127.0.0.1, so the peer
        # check passes, but the request is addressed to an attacker's domain.
        self.assertEqual(
            self.request("/api/meta", headers={"Host": "evil.example"}), 403)

    def test_localhost_is_accepted(self):
        for host in (f"localhost:{self.port}", f"127.0.0.1:{self.port}"):
            with self.subTest(host=host):
                self.assertEqual(self.request("/api/meta",
                                              headers={"Host": host}), 200)


class TestSyncEndpoint(GuardCase):
    def test_a_cross_origin_post_is_refused(self):
        """The one that matters: a page you visit must not be able to sync."""
        self.assertEqual(
            self.request("/api/sync", "POST",
                         {"Origin": "https://evil.example"}), 403)

    def test_a_rebound_host_cannot_reach_sync(self):
        self.assertEqual(
            self.request("/api/sync", "POST", {"Host": "evil.example"}), 403)

    def test_same_origin_is_allowed_through_the_guards(self):
        """A request from the admin page must not be blocked.

        This is the other half: guards that also stop legitimate use are a bug.
        Only the guards are under test, so any status other than 403 means the
        request got past them and into the handler.
        """
        status = self.request("/api/sync", "POST",
                              {"Origin": f"http://127.0.0.1:{self.port}"})
        self.assertNotEqual(status, 403)

    def test_sync_is_the_only_post_route(self):
        self.assertEqual(self.request("/api/anything", "POST"), 404)


if __name__ == "__main__":
    unittest.main(verbosity=2)
