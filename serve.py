#!/usr/bin/env python3
"""Local web server for FindMaxScreen.

Serves the single-page UI from web/ and a small read-only JSON API on top of
theatres.sqlite3.  Binds to loopback only; every query is parameterised and every
GET uses a read-only SQLite connection, so the page cannot damage the database.
The one exception is POST /api/sync, which shells out to sync.py to refresh.

    ./serve.py               # http://127.0.0.1:8787
    ./serve.py --port 9000 --no-open
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import re
import sqlite3
import subprocess
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

HERE = Path(__file__).resolve().parent
WEB_DIR = HERE / "web"
DEFAULT_DB = HERE / "theatres.sqlite3"

MAX_LIMIT = 2000
SYNC_TIMEOUT = 180

VENUE_COLUMNS = (
    "id", "region", "country", "state", "city", "name", "maps_url",
    "screen_ar", "digital_projector", "projector_family", "max_digital_ar",
    "film_projector", "film_family", "has_70mm", "is_dome", "is_1_43",
    "is_temporary", "commercial_films", "screen_w_m", "screen_h_m",
    "screen_area_m2", "dimensions_raw", "data_notes", "removed_at",
)

SORTS = {
    # Screen area is the interesting "biggest first" sort; NULLs go last.
    "size": "v.screen_area_m2 IS NULL, v.screen_area_m2 DESC, v.name",
    "name": "v.name COLLATE NOCASE",
    "location": "v.country, v.state, v.city, v.name",
}
DEFAULT_SORT = "location"

_TOKEN_RE = re.compile(r"[^\W_]+", re.UNICODE)


def fts_match(query: str) -> str | None:
    """Turn free text into a safe FTS5 prefix query ("van cou" -> van* AND cou*)."""
    tokens = _TOKEN_RE.findall(query)
    if not tokens:
        return None
    return " AND ".join(f'"{t}"*' for t in tokens)


class VenueStore:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self._local = threading.local()

    def conn(self) -> sqlite3.Connection:
        # sqlite3 connections are not shareable across threads, and this server
        # is threaded; keep one read-only connection per thread.
        conn = getattr(self._local, "conn", None)
        if conn is None:
            uri = f"file:{self.db_path}?mode=ro"
            conn = sqlite3.connect(uri, uri=True)
            conn.row_factory = sqlite3.Row
            self._local.conn = conn
        return conn

    def venues(self, params: dict[str, list[str]]) -> dict:
        def one(key: str, default: str = "") -> str:
            return (params.get(key) or [default])[0].strip()

        def flag(key: str) -> bool:
            return one(key) in ("1", "true", "yes", "on")

        wheres: list[str] = []
        args: list = []

        match = fts_match(one("q"))
        if match:
            source = "FROM venues_fts JOIN venues v ON v.id = venues_fts.rowid"
            wheres.append("venues_fts MATCH ?")
            args.append(match)
        else:
            source = "FROM venues v"

        for key, column in (("country", "v.country"), ("region", "v.region"),
                            ("projector", "v.projector_family"),
                            ("film", "v.film_family")):
            value = one(key)
            if value:
                wheres.append(f"{column} = ?")
                args.append(value)

        if flag("film70"):
            wheres.append("v.has_70mm = 1")
        if flag("dome"):
            wheres.append("v.is_dome = 1")
        if flag("commercial"):
            wheres.append("v.commercial_films = 1")

        ar = one("ar")
        if ar == "1.43":
            wheres.append("v.is_1_43 = 1")
        elif ar:
            wheres.append("v.screen_ar LIKE ?")
            args.append(f"{ar}%")

        if not flag("include_removed"):
            wheres.append("v.removed_at IS NULL")

        sort = one("sort", DEFAULT_SORT)
        if sort == "relevance" and match:
            order = "rank"
        else:
            order = SORTS.get(sort, SORTS[DEFAULT_SORT])

        try:
            limit = min(int(one("limit", "1000")), MAX_LIMIT)
        except ValueError:
            limit = 1000

        clause = (" WHERE " + " AND ".join(wheres)) if wheres else ""
        total = self.conn().execute(
            f"SELECT count(*) {source}{clause}", args).fetchone()[0]
        rows = self.conn().execute(
            f"SELECT {', '.join('v.' + c for c in VENUE_COLUMNS)} {source}{clause}"
            f" ORDER BY {order} LIMIT ?", args + [limit]).fetchall()
        return {"total": total, "shown": len(rows),
                "venues": [dict(r) for r in rows]}

    def facets(self) -> dict:
        def counts(column: str) -> list[dict]:
            rows = self.conn().execute(
                f"SELECT {column} AS value, count(*) AS n FROM venues"
                f" WHERE removed_at IS NULL AND {column} != ''"
                f" GROUP BY value ORDER BY value COLLATE NOCASE", ()).fetchall()
            return [dict(r) for r in rows]

        # The aspect-ratio facet counts the flag rather than the raw string, so
        # the dropdown's number is the number the filter will actually return -
        # "Dome 1.43:1" and "1.43:1" are one choice, not two.
        ratios = self.conn().execute(
            "SELECT sum(is_1_43) AS ar_1_43,"
            " sum(screen_ar LIKE '1.90%') AS ar_1_90"
            " FROM venues WHERE removed_at IS NULL").fetchone()

        return {
            "countries": counts("country"),
            "regions": counts("region"),
            "projectors": counts("projector_family"),
            "films": counts("film_family"),
            "ars": [{"value": "1.43", "n": ratios["ar_1_43"] or 0},
                    {"value": "1.90", "n": ratios["ar_1_90"] or 0}],
            "film70": self.conn().execute(
                "SELECT count(*) FROM venues WHERE has_70mm = 1"
                " AND removed_at IS NULL").fetchone()[0],
        }

    def meta(self) -> dict:
        rev = self.conn().execute(
            "SELECT * FROM revisions ORDER BY fetched_at DESC, revid DESC LIMIT 1"
        ).fetchone()
        stats = self.conn().execute(
            "SELECT count(*) AS venues,"
            " sum(has_70mm) AS film70, sum(is_dome) AS dome,"
            " count(DISTINCT country) AS countries"
            " FROM venues WHERE removed_at IS NULL").fetchone()
        removed = self.conn().execute(
            "SELECT count(*) FROM venues WHERE removed_at IS NOT NULL").fetchone()[0]
        return {
            "revision": dict(rev) if rev else None,
            "stats": dict(stats) | {"removed": removed},
        }

    def recent_changes(self, limit: int = 50) -> dict:
        rows = self.conn().execute(
            "SELECT c.changed_at, c.revid, c.field, c.old_value, c.new_value,"
            " v.name, v.city, v.country FROM venue_changes c"
            " JOIN venues v ON v.id = c.venue_id"
            " ORDER BY c.id DESC LIMIT ?", (limit,)).fetchall()
        return {"changes": [dict(r) for r in rows]}


class Handler(BaseHTTPRequestHandler):
    server_version = "findmaxscreen/1.0"
    store: VenueStore
    db_path: Path

    def log_message(self, fmt, *args):  # quieter than the default
        if not self.path.startswith("/api/") or self.command == "POST":
            sys.stderr.write("  %s %s\n" % (self.command, self.path))

    # -- helpers ---------------------------------------------------------- #

    def _send(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, payload: dict, code: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode()
        self._send(code, body, "application/json; charset=utf-8")

    def _is_local(self) -> bool:
        return self.client_address[0] in ("127.0.0.1", "::1")

    def _host_ok(self) -> bool:
        """Reject a request addressed to anything but loopback.

        Defeats DNS rebinding: an attacker points evil.example at 127.0.0.1, so
        the connection genuinely *is* local and passes _is_local(), but the Host
        header still says evil.example. Pinning the host closes that.
        """
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]")
        return host in ("127.0.0.1", "localhost", "::1", "")

    def _origin_ok(self) -> bool:
        """Reject a state-changing request initiated by another website.

        A browser will happily *send* a cross-origin POST even though the reply
        is unreadable, so any page you visit while this server is running could
        otherwise fire off a sync. Same-origin requests either omit Origin or
        send our own; anything else is somebody else's page talking.
        """
        origin = self.headers.get("Origin")
        if origin is None:
            return True
        host = urlparse(origin).hostname
        return host in ("127.0.0.1", "localhost", "::1")

    def _static(self, path: str) -> None:
        rel = "index.html" if path in ("/", "") else path.lstrip("/")
        target = (WEB_DIR / rel).resolve()
        if not str(target).startswith(str(WEB_DIR.resolve())) or not target.is_file():
            self._send(404, b"not found", "text/plain; charset=utf-8")
            return
        ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype.endswith("javascript"):
            ctype += "; charset=utf-8"
        self._send(200, target.read_bytes(), ctype)

    # -- routes ----------------------------------------------------------- #

    def do_GET(self) -> None:
        if not self._is_local() or not self._host_ok():
            self._send(403, b"loopback only", "text/plain; charset=utf-8")
            return
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)
        try:
            if parsed.path == "/api/venues":
                self._json(self.store.venues(params))
            elif parsed.path == "/api/facets":
                self._json(self.store.facets())
            elif parsed.path == "/api/meta":
                self._json(self.store.meta())
            elif parsed.path == "/api/changes":
                self._json(self.store.recent_changes())
            elif parsed.path.startswith("/api/"):
                self._json({"error": "unknown endpoint"}, 404)
            else:
                self._static(parsed.path)
        except sqlite3.OperationalError as exc:
            self._json({"error": f"database not ready: {exc}. Run ./sync.py first."}, 503)
        except Exception as exc:  # keep the server alive, report the failure
            self._json({"error": f"{type(exc).__name__}: {exc}"}, 500)

    do_HEAD = do_GET

    def do_POST(self) -> None:
        # POST is the only thing here that changes state - it shells out to
        # sync.py - so it gets the strictest check.
        if not self._is_local() or not self._host_ok():
            self._send(403, b"loopback only", "text/plain; charset=utf-8")
            return
        if not self._origin_ok():
            self._send(403, b"cross-origin requests are refused",
                       "text/plain; charset=utf-8")
            return
        if urlparse(self.path).path != "/api/sync":
            self._json({"error": "unknown endpoint"}, 404)
            return
        try:
            proc = subprocess.run(
                [sys.executable, str(HERE / "sync.py"), "--db", str(self.db_path), "--json"],
                capture_output=True, text=True, timeout=SYNC_TIMEOUT)
        except subprocess.TimeoutExpired:
            self._json({"error": f"sync timed out after {SYNC_TIMEOUT}s"}, 504)
            return
        try:
            payload = json.loads(proc.stdout.strip().splitlines()[-1])
        except (ValueError, IndexError):
            self._json({"error": "sync produced no summary",
                        "stdout": proc.stdout, "stderr": proc.stderr[-2000:]}, 500)
            return
        payload["exit_code"] = proc.returncode
        self._json(payload)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--no-open", action="store_true",
                        help="do not open a browser window")
    args = parser.parse_args(argv)

    if not args.db.exists():
        print(f"no database at {args.db} - run ./sync.py first", file=sys.stderr)
        return 1

    Handler.store = VenueStore(args.db)
    Handler.db_path = args.db
    httpd = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    url = f"http://127.0.0.1:{args.port}"
    print(f"FindMaxScreen -> {url}   (ctrl-c to stop)")
    if not args.no_open:
        threading.Timer(0.4, webbrowser.open, (url,)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
