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

import json
import shutil
import socket
import subprocess
import tempfile
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


SYNC_CALLS: list[list[str]] = []

# What sync.py prints on its last line when the wiki has not moved. The stub
# returns a real summary rather than an empty stdout so the handler takes its
# success path and the test sees the status a working sync would produce.
SYNC_SUMMARY = json.dumps({"status": "current", "revid": 2938, "venues": 482,
                           "added": 0, "removed": 0, "changed": 0, "log": []})


def recording_sync(self):
    """Stand in for the subprocess, recording the command it replaced."""
    SYNC_CALLS.append(self.sync_argv())
    return subprocess.CompletedProcess(SYNC_CALLS[-1], 0, SYNC_SUMMARY + "\n", "")


class TestSyncEndpoint(GuardCase):
    """The sync guards, with the sync itself stubbed out.

    `POST /api/sync` shells out to sync.py, which fetches the live wiki and
    rewrites the database. Until this class started replacing it, the
    same-origin case below ran that for real against the committed
    theatres.sqlite3 - so gate 1 of the daily job was quietly syncing
    production data, and the job's own sync step then found the revision
    already applied ("already at revision 2938, nothing to do"). A revision
    could reach the database through a path that archives no snapshot, reports
    no diff, and leaves nothing to commit.

    Stubbing it buys more than speed and an offline test run. A status code
    only says the handler answered; what these guards exist to prevent is the
    side effect, and the stub makes the side effect directly observable. The
    two refusals below now assert that no sync was even attempted, which is
    the property that actually matters and which a 403 alone never proved.

    The database still points at a throwaway copy, so the command recorded
    here names a scratch file and this suite cannot touch the real one even if
    the stub is later removed.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._tmpdir = tempfile.TemporaryDirectory()
        scratch = Path(cls._tmpdir.name) / DB.name
        shutil.copy(DB, scratch)
        cls._real_db = serve.Handler.db_path
        serve.Handler.db_path = scratch
        cls._real_run_sync = serve.Handler._run_sync
        serve.Handler._run_sync = recording_sync

    @classmethod
    def tearDownClass(cls):
        serve.Handler._run_sync = cls._real_run_sync
        serve.Handler.db_path = cls._real_db
        cls._tmpdir.cleanup()
        super().tearDownClass()

    def setUp(self):
        SYNC_CALLS.clear()

    def test_a_cross_origin_post_is_refused(self):
        """The one that matters: a page you visit must not be able to sync."""
        self.assertEqual(
            self.request("/api/sync", "POST",
                         {"Origin": "https://evil.example"}), 403)
        self.assertEqual(SYNC_CALLS, [], "a refused request still ran a sync")

    def test_a_rebound_host_cannot_reach_sync(self):
        self.assertEqual(
            self.request("/api/sync", "POST", {"Host": "evil.example"}), 403)
        self.assertEqual(SYNC_CALLS, [], "a refused request still ran a sync")

    def test_same_origin_is_allowed_through_the_guards(self):
        """A request from the admin page must not be blocked.

        This is the other half: guards that also stop legitimate use are a bug.
        """
        status = self.request("/api/sync", "POST",
                              {"Origin": f"http://127.0.0.1:{self.port}"})
        self.assertNotEqual(status, 403)
        self.assertEqual(len(SYNC_CALLS), 1, "the request never reached the sync")

    def test_the_stubbed_command_is_the_one_that_would_have_run(self):
        """Guard against the stub drifting from the code it stands in for.

        A stub that no longer resembles the real invocation would keep passing
        while the thing it models had changed underneath it, so assert the
        argv the handler builds - including that it names the database it was
        given, which is what kept this suite off the committed file.
        """
        self.request("/api/sync", "POST",
                     {"Origin": f"http://127.0.0.1:{self.port}"})
        argv = SYNC_CALLS[0]
        self.assertEqual(argv[1], str(serve.HERE / "sync.py"))
        self.assertEqual(argv[2:], ["--db", str(serve.Handler.db_path), "--json"])
        self.assertNotEqual(Path(argv[3]), DB, "a test pointed sync at the real database")

    def test_sync_is_the_only_post_route(self):
        self.assertEqual(self.request("/api/anything", "POST"), 404)


if __name__ == "__main__":
    unittest.main(verbosity=2)
