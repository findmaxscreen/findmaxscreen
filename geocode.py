#!/usr/bin/env python3
"""Attach coordinates to venues using OpenStreetMap's Nominatim service.

The wiki carries no coordinates, so a map or a directions link needs them from
somewhere else.  Nominatim is used rather than Google Geocoding for a licensing
reason, not a technical one: Google's terms cap caching at 30 days and tie
indefinite storage of lat/lng to their own map surface, whereas OSM data is ODbL
and may be stored permanently as long as the attribution stays on the page.
That matters here because the whole point is to bake the coordinates into a
static file and serve them forever.

Nominatim's usage policy allows this kind of one-off bulk run provided requests
stay at or under one per second and carry a real User-Agent.  Both are enforced
below, and results are written straight to the database so a re-run only fetches
what is still missing.

    ./geocode.py                # fill in venues that have no coordinates yet
    ./geocode.py --limit 20     # try a handful first
    ./geocode.py --dry-run      # look them up, write nothing
    ./geocode.py --force        # re-geocode everything, including hits
    ./geocode.py --report       # just show current coverage
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import sync
from export import COUNTRY_CODES

HERE = Path(__file__).resolve().parent
DEFAULT_DB = HERE / "theatres.sqlite3"

ENDPOINT = "https://nominatim.openstreetmap.org/search"
USER_AGENT = "findmaxscreen/1.0 (one-off venue geocoding; contact via github)"

# Nominatim asks for at most one request per second. Going under that is rude
# and gets you blocked; there is no rush on a job that runs once.
MIN_INTERVAL = 1.1

# Sanity bounds for a returned point, checked before it is stored.
MAX_KM_FROM_CITY = 60.0


class GeocodeError(RuntimeError):
    pass


# --------------------------------------------------------------------------- #
# Nominatim
# --------------------------------------------------------------------------- #

_last_request = 0.0


def _throttled_get(params: dict, timeout: int = 30) -> list[dict]:
    global _last_request
    wait = MIN_INTERVAL - (time.monotonic() - _last_request)
    if wait > 0:
        time.sleep(wait)

    req = Request(f"{ENDPOINT}?{urlencode(params)}", headers={
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
    })
    try:
        with urlopen(req, timeout=timeout) as resp:
            payload = json.load(resp)
    except HTTPError as exc:
        if exc.code == 429:
            raise GeocodeError(
                "Nominatim returned 429 (rate limited). Wait a few minutes and "
                "re-run; already-geocoded venues are skipped.") from exc
        raise GeocodeError(f"HTTP {exc.code} from Nominatim") from exc
    except URLError as exc:
        raise GeocodeError(f"cannot reach Nominatim: {exc.reason}") from exc
    finally:
        _last_request = time.monotonic()

    return payload if isinstance(payload, list) else []


def lookup(query: str, country_code: str = "") -> dict | None:
    """One Nominatim search, constrained to a country when we know it."""
    # extratags carries the venue's own website, which OSM licenses under ODbL
    # and we may therefore store and republish - unlike a search result.
    params = {"q": query, "format": "jsonv2", "limit": "1", "extratags": "1"}
    if country_code:
        # Constraining by country is the single biggest accuracy win: it stops
        # "Kinepolis Brussels" matching a Kinepolis in another country.
        params["countrycodes"] = country_code.lower()
    results = _throttled_get(params)
    if not results:
        return None
    hit = results[0]
    tags = hit.get("extratags") or {}
    return {"lat": float(hit["lat"]), "lon": float(hit["lon"]),
            "display_name": hit.get("display_name", ""),
            "website": (tags.get("website") or tags.get("contact:website")
                        or "").strip()}


# --------------------------------------------------------------------------- #
# Geocoding one venue
# --------------------------------------------------------------------------- #

def _haversine_km(a_lat, a_lon, b_lat, b_lon) -> float:
    from math import asin, cos, radians, sin, sqrt
    dlat, dlon = radians(b_lat - a_lat), radians(b_lon - a_lon)
    h = (sin(dlat / 2) ** 2
         + cos(radians(a_lat)) * cos(radians(b_lat)) * sin(dlon / 2) ** 2)
    return 2 * 6371.0 * asin(sqrt(h))


_IMAX_SUFFIX = re.compile(r"\s*[&+]\s*IMAX\b.*$", re.I)
_IMAX_PREFIX = re.compile(r"^IMAX(\s*3D)?\s*[,:]\s*", re.I)
_PARENTHETICAL = re.compile(r"\s*\([^)]*\)\s*$")
_BARE_IMAX = re.compile(r"\s*\bIMAX\b\s*$", re.I)


def search_names(name: str) -> list[str]:
    """Query forms to try for a theatre, best first.

    OSM records cinemas under their trading name, not the wiki's decorated one:
    "Dendy Canberra & IMAX" is mapped as "Dendy Cinema", and "IMAX, Melbourne
    Museum" as "Melbourne Museum".  Every name tried verbatim missed; stripping
    the IMAX ornamentation found all of them.
    """
    stripped = _PARENTHETICAL.sub("", name)
    stripped = _IMAX_SUFFIX.sub("", stripped)
    stripped = _IMAX_PREFIX.sub("", stripped)
    stripped = _BARE_IMAX.sub("", stripped).strip(" ,&")

    forms = []
    for candidate in (stripped, name):
        if candidate and candidate not in forms:
            forms.append(candidate)
    return forms


def geocode_venue(row: sqlite3.Row) -> dict:
    """Resolve one venue, degrading from an exact address to the city centre.

    Returns {lat, lon, precision, note}. `precision` is "venue" when the
    theatre itself was found, "city" when only the town was, and "none" when
    even that failed - which is recorded rather than left blank so a re-run
    does not keep retrying hopeless rows forever.
    """
    code = COUNTRY_CODES_BY_NAME.get(row["country"], "")
    city_bits = [row["city"], row["state"], row["country"]]
    city_query = ", ".join(p for p in city_bits if p)

    # The city first: it is both a fallback and a sanity check for the venue hit.
    city = lookup(city_query, code)

    venue = None
    for form in search_names(row["name"]):
        query = ", ".join(p for p in (form, row["city"], row["country"]) if p)
        venue = lookup(query, code)
        if venue:
            break

    if venue:
        # A named-venue hit that lands 60 km from its own city is the wrong
        # place with a similar name, which is the usual Nominatim failure.
        if city:
            distance = _haversine_km(venue["lat"], venue["lon"],
                                     city["lat"], city["lon"])
            if distance > MAX_KM_FROM_CITY:
                return {"lat": city["lat"], "lon": city["lon"],
                        "precision": "city",
                        "matched": city["display_name"],
                        "note": f"venue match was {distance:.0f} km from "
                                f"{row['city']}; used the city instead"}
        return {"lat": venue["lat"], "lon": venue["lon"],
                "precision": "venue", "website": venue.get("website", ""),
                "matched": venue["display_name"],
                "note": venue["display_name"]}

    if city:
        return {"lat": city["lat"], "lon": city["lon"], "precision": "city",
                "matched": city["display_name"],
                "note": f"theatre not found; centred on {row['city']}"}

    return {"lat": None, "lon": None, "precision": "none",
            "note": "no match for the theatre or its city"}


COUNTRY_CODES_BY_NAME = {name: code for code, name in COUNTRY_CODES.items()}


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def report(conn: sqlite3.Connection) -> None:
    rows = conn.execute(
        "SELECT geo_precision AS p, count(*) AS n FROM venues"
        " WHERE removed_at IS NULL GROUP BY p ORDER BY n DESC").fetchall()
    total = sum(r["n"] for r in rows)
    print(f"{total} live venues")
    for row in rows:
        print(f"  {row['p'] or '(not geocoded)':<16} {row['n']:>4}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--limit", type=int, default=0,
                        help="stop after this many lookups")
    parser.add_argument("--force", action="store_true",
                        help="re-geocode venues that already have coordinates")
    parser.add_argument("--dry-run", action="store_true",
                        help="look up and report; write nothing")
    parser.add_argument("--report", action="store_true",
                        help="show coverage and exit")
    args = parser.parse_args(argv)

    if not args.db.exists():
        print(f"no database at {args.db} - run ./sync.py first", file=sys.stderr)
        return 1

    conn = sync.connect(args.db)

    if args.report:
        report(conn)
        return 0

    where = "" if args.force else " AND geo_precision = ''"
    todo = conn.execute(
        f"SELECT * FROM venues WHERE removed_at IS NULL{where}"
        " ORDER BY country, city, name").fetchall()
    if args.limit:
        todo = todo[:args.limit]

    if not todo:
        print("nothing to geocode; every live venue already has a result.")
        report(conn)
        return 0

    print(f"geocoding {len(todo)} venue(s) via Nominatim at "
          f"{MIN_INTERVAL:.1f}s intervals — about "
          f"{len(todo) * MIN_INTERVAL * 2 / 60:.0f} min\n")

    tally = {"venue": 0, "city": 0, "none": 0}
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    for i, row in enumerate(todo, 1):
        try:
            result = geocode_venue(row)
        except GeocodeError as exc:
            print(f"\nstopped: {exc}", file=sys.stderr)
            conn.commit()
            report(conn)
            return 2

        tally[result["precision"]] += 1
        mark = {"venue": "ok  ", "city": "city", "none": "MISS"}[result["precision"]]
        print(f"  [{i:>3}/{len(todo)}] {mark} {row['name'][:44]:<44} "
              f"{row['city']}, {row['country']}")

        if not args.dry_run:
            conn.execute(
                "UPDATE venues SET lat = ?, lon = ?, geo_source = ?,"
                " geo_precision = ?, geo_matched = ?, geocoded_at = ?"
                " WHERE id = ?",
                (result["lat"], result["lon"], "nominatim",
                 result["precision"], result.get("matched", ""), now,
                 row["id"]))
            if result.get("website"):
                conn.execute("UPDATE venues SET website = ? WHERE id = ?",
                             (result["website"], row["id"]))
            if i % 20 == 0:
                conn.commit()

    if not args.dry_run:
        conn.commit()

    print(f"\n{tally['venue']} exact · {tally['city']} city-level · "
          f"{tally['none']} not found")
    if args.dry_run:
        print("dry run: nothing written")
    else:
        print("\nRun ./export.py to publish the coordinates.")
    report(conn)
    return 0


if __name__ == "__main__":
    sys.exit(main())
