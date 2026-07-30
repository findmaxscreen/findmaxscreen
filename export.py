#!/usr/bin/env python3
"""Build the publishable static site from theatres.sqlite3.

The public site is plain files: no server, no database, no API.  Everything the
page needs - venues, facet counts, revision metadata and the timezone lookup
used to guess a visitor's country - is baked into one JSON document, and the
browser does the searching and filtering.  476 venues is small enough that this
is both simpler and faster than talking to a backend.

The bundle is assembled from an explicit allow-list, so the admin page is
structurally incapable of reaching the public site: it is not that admin.html is
hidden, it is that it is never copied.

    ./export.py              # write web/data/venues.json and build dist/
    ./export.py --data-only  # refresh the JSON in place, skip dist/
    ./export.py --check      # report what would be written, write nothing
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import sync

HERE = Path(__file__).resolve().parent
DEFAULT_DB = HERE / "theatres.sqlite3"
WEB_DIR = HERE / "web"
DATA_FILE = WEB_DIR / "data" / "venues.json"
DIST_DIR = HERE / "dist"

# Exactly what gets published.  Anything not named here stays local - which is
# the entire security model for the admin page.
PUBLIC_FILES = ("index.html", "app.js", "query.js", "geo.js", "style.css",
                # Link-preview assets. index.html points at both by absolute
                # URL, so a deploy that drops them ships tags aimed at two 404s
                # - and Apple caches that failure per URL. They are required,
                # not optional, for exactly that reason.
                "og.png", "apple-touch-icon.png")
PUBLIC_DATA = ("data/venues.json",)

# Shipped when present, but their absence is not an error.  GitHub Pages reads
# the custom domain from a CNAME file *inside the published artifact* and drops
# the domain on any deploy that omits it - so it has to travel with the bundle,
# not just live in the repo settings.
OPTIONAL_FILES = ("CNAME",)

# Where an ordinary visitor reports a problem. A static site has nowhere to POST
# a form to, and a form service would mean a third party plus an endpoint id in
# the page. A mailto on our own domain needs neither, works for someone who has
# never heard of GitHub, and being a role address rather than a personal one, it
# can be filtered or replaced without touching anybody's inbox identity.
#
# The trade is that a published address gets scraped and will attract spam. That
# is the price of not requiring an account to say "this link is broken".
REPORT_EMAIL = "issues@findmaxscreen.com"

# The developer path, for people who would rather file an issue than write an
# email. Empty until the repository exists; the UI omits it rather than shipping
# a dead link. No trailing slash — app.js appends /issues/new to it.
REPO_URL = "https://github.com/findmaxscreen/findmaxscreen"

# The wiki is the actual source of truth for venue data. A correction made there
# reaches everyone and flows back here on the next sync, so the report flow
# should point at it rather than quietly hoarding fixes in an issue tracker.
WIKI_URL = "https://imax.fandom.com/wiki/List_of_IMAX_venues"

# Files that must never reach dist/, checked explicitly after the copy so a
# future rename cannot quietly start publishing the admin tools.
PRIVATE_PATTERNS = ("admin", "sync.py", "serve.py", ".sqlite3")

# Columns the page actually uses.  Provenance and raw wiki text stay behind.
# Trimmed to what the page actually reads. Four columns were dropped after
# measuring: maps_url (55 KB) is a deterministic Google Maps search URL the
# browser rebuilds from name/city/country; digital_projector (15 KB) was only
# ever tested for truthiness, which projector_family already answers; and
# max_digital_ar and film_projector (22 KB) were referenced nowhere at all.
# Together roughly a third of the payload, for no loss of function.
EXPORT_COLUMNS = (
    "region", "country", "state", "city", "name",
    "screen_ar", "projector_family",
    "film_family", "has_70mm", "is_dome", "is_1_43",
    "is_temporary", "commercial_films", "screen_w_m", "screen_h_m",
    "screen_area_m2", "data_notes", "removed_at",
    # From OpenStreetMap, not the wiki. geo_precision says how much to trust
    # them: "venue" is the theatre, "city" is only the town centre.
    "lat", "lon", "geo_precision", "website",
)

# ISO 3166-1 alpha-2 for every country in the dataset.  The browser turns a
# timezone into one of these codes; this is what maps it back to the spelling
# the wiki uses, so "IN" finds the venues filed under "India".
COUNTRY_CODES = {
    "AW": "Aruba", "AU": "Australia", "AT": "Austria", "BS": "Bahamas",
    "BH": "Bahrain", "BE": "Belgium", "BR": "Brazil", "CA": "Canada",
    "CN": "China", "CO": "Colombia", "CW": "Curaçao", "CZ": "Czechia",
    "EC": "Ecuador", "FI": "Finland", "FR": "France", "DE": "Germany",
    "GR": "Greece", "HK": "Hong Kong", "IN": "India", "ID": "Indonesia",
    "IE": "Ireland", "IT": "Italy", "JP": "Japan", "XK": "Kosovo",
    "KW": "Kuwait", "LV": "Latvia", "LT": "Lithuania", "LU": "Luxembourg",
    "MY": "Malaysia", "MX": "Mexico", "MA": "Morocco", "NL": "Netherlands",
    "NZ": "New Zealand", "NO": "Norway", "OM": "Oman", "PE": "Peru",
    "PH": "Philippines", "PL": "Poland", "PT": "Portugal", "QA": "Qatar",
    "SA": "Saudi Arabia", "RS": "Serbia", "SG": "Singapore",
    "ZA": "South Africa", "KR": "South Korea", "ES": "Spain",
    "LK": "Sri Lanka", "SE": "Sweden", "CH": "Switzerland", "TW": "Taiwan",
    "TH": "Thailand", "UA": "Ukraine", "AE": "United Arab Emirates",
    "GB": "United Kingdom", "US": "United States", "VN": "Vietnam",
}

# Zones browsers still report under their pre-rename names.  zone.tab lists
# only the current spelling, so a visitor in Kyiv or Kolkata would otherwise
# fall through to the language guess.
TZ_ALIASES = {
    "Asia/Calcutta": "Asia/Kolkata",
    "Asia/Katmandu": "Asia/Kathmandu",
    "Asia/Rangoon": "Asia/Yangon",
    "Asia/Saigon": "Asia/Ho_Chi_Minh",
    "Europe/Kiev": "Europe/Kyiv",
    "Australia/Canberra": "Australia/Sydney",
    "America/Buenos_Aires": "America/Argentina/Buenos_Aires",
    "Pacific/Ponape": "Pacific/Pohnpei",
    "Atlantic/Faeroe": "Atlantic/Faroe",
}

ZONE_TABS = (
    Path("/usr/share/zoneinfo/zone1970.tab"),
    Path("/usr/share/zoneinfo/zone.tab"),
)


class ExportError(RuntimeError):
    pass


# --------------------------------------------------------------------------- #
# Timezone -> country
# --------------------------------------------------------------------------- #

def timezone_map() -> dict[str, str]:
    """Map every IANA timezone to an ISO country code, read from the OS.

    Shipping this table means the page can name a visitor's country with no
    network call, no permission prompt and no IP leaving the browser.  It is
    read from the system tz database rather than hand-written so it stays
    correct as zones are added and renamed.
    """
    table = next((p for p in ZONE_TABS if p.exists()), None)
    if table is None:
        raise ExportError(
            "no zone.tab found under /usr/share/zoneinfo; cannot build the "
            "timezone map. Pass --data-only on a machine that has one, or the "
            "page will fall back to the browser language.")

    zones: dict[str, str] = {}
    for line in table.read_text().splitlines():
        if line.startswith("#") or not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        # zone1970.tab allows several countries per zone ("CH,DE,LI"); the
        # first is the one the zone is named for.
        code = parts[0].split(",")[0].strip()
        zone = parts[2].strip()
        if len(code) == 2 and zone:
            zones[zone] = code

    for alias, canonical in TZ_ALIASES.items():
        if canonical in zones:
            zones[alias] = zones[canonical]

    return dict(sorted(zones.items()))


# --------------------------------------------------------------------------- #
# Database -> JSON
# --------------------------------------------------------------------------- #

def _rounded(value):
    # Trim float noise so the JSON stays byte-stable between exports of the
    # same data; a diff should mean the wiki changed, not that Python printed
    # 533.0000000001 this time.
    return round(value, 4) if isinstance(value, float) else value


def build_payload(conn: sqlite3.Connection) -> dict:
    conn.row_factory = sqlite3.Row

    venues = [
        {k: _rounded(row[k]) for k in EXPORT_COLUMNS}
        for row in conn.execute(
            f"SELECT {', '.join(EXPORT_COLUMNS)} FROM venues"
            " ORDER BY country, state, city, name")
    ]
    live = [v for v in venues if v["removed_at"] is None]

    def counts(field: str) -> list[dict]:
        tally: dict[str, int] = {}
        for venue in live:
            value = venue[field]
            if value:
                tally[value] = tally.get(value, 0) + 1
        return [{"value": k, "n": tally[k]}
                for k in sorted(tally, key=str.casefold)]

    revision = conn.execute(
        "SELECT revid, wiki_timestamp, fetched_at FROM revisions"
        " ORDER BY fetched_at DESC, revid DESC LIMIT 1").fetchone()

    countries = counts("country")

    # If the wiki adds a country we have no ISO code for, the "your country"
    # section silently stops recognising visitors there.  Say so rather than
    # letting it rot.
    known = set(COUNTRY_CODES.values())
    unmapped = sorted(c["value"] for c in countries if c["value"] not in known)

    return {
        "unmapped_countries": unmapped,
        "links": {"repo": REPO_URL, "wiki": WIKI_URL, "email": REPORT_EMAIL},
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "revision": dict(revision) if revision else None,
        "stats": {
            "venues": len(live),
            "countries": len(countries),
            "film70": sum(v["has_70mm"] for v in live),
            "dome": sum(v["is_dome"] for v in live),
            "removed": len(venues) - len(live),
        },
        "facets": {
            "countries": countries,
            "regions": counts("region"),
            "projectors": counts("projector_family"),
            "films": counts("film_family"),
            "ars": [
                {"value": "1.43", "n": sum(v["is_1_43"] for v in live)},
                {"value": "1.90",
                 "n": sum(v["screen_ar"].startswith("1.90") for v in live)},
            ],
            "film70": sum(v["has_70mm"] for v in live),
        },
        "geo": {
            "timezones": timezone_map(),
            "countries": COUNTRY_CODES,
        },
        "venues": venues,
    }


# --------------------------------------------------------------------------- #
# dist/
# --------------------------------------------------------------------------- #

def build_dist(dist: Path = DIST_DIR) -> list[Path]:
    """Copy the public files into a clean dist/, then prove nothing else got in."""
    if dist.exists():
        shutil.rmtree(dist)
    dist.mkdir(parents=True)

    written: list[Path] = []
    for name in PUBLIC_FILES:
        source = WEB_DIR / name
        if not source.is_file():
            raise ExportError(f"missing public file {source}")
        shutil.copy2(source, dist / name)
        written.append(dist / name)

    for name in OPTIONAL_FILES:
        source = WEB_DIR / name
        if source.is_file():
            shutil.copy2(source, dist / name)
            written.append(dist / name)

    for name in PUBLIC_DATA:
        source = WEB_DIR / name
        if not source.is_file():
            raise ExportError(f"missing {source}; run ./export.py first")
        target = dist / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        written.append(target)

    # The allow-list above should make this impossible.  Check anyway: this is
    # the assertion that keeps the admin page off the public internet.
    leaked = [
        p for p in dist.rglob("*") if p.is_file()
        and any(pattern in p.name.lower() for pattern in PRIVATE_PATTERNS)
    ]
    if leaked:
        raise ExportError("refusing to publish; private files reached dist/: "
                          + ", ".join(str(p.relative_to(dist)) for p in leaked))
    return written


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--dist", type=Path, default=DIST_DIR)
    parser.add_argument("--data-only", action="store_true",
                        help="write the JSON only; do not assemble dist/")
    parser.add_argument("--check", action="store_true",
                        help="report what would be written, write nothing")
    parser.add_argument("--allow-invalid", action="store_true",
                        help="export even if the database fails validation")
    args = parser.parse_args(argv)

    if not args.db.exists():
        print(f"no database at {args.db} - run ./sync.py first", file=sys.stderr)
        return 1

    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row

    # Publishing a database that fails its own checks would push a known-wrong
    # dataset to the internet, so the export refuses by default.
    findings = sync.validate(conn)
    errors = [f for f in findings if f["severity"] == "ERROR"]
    for finding in findings:
        print(f"  {finding['severity']:<5} {finding['check']} "
              f"({finding['count']}) - {finding['explanation']}")
    if errors and not args.allow_invalid:
        print(f"\nREFUSING to export: {len(errors)} validation error(s). "
              f"Fix the database, or re-run with --allow-invalid.", file=sys.stderr)
        return 2

    payload = build_payload(conn)
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    stats = payload["stats"]

    print(f"{stats['venues']} venues · {stats['countries']} countries · "
          f"{stats['film70']} with 15/70 mm film · {stats['removed']} de-listed")
    print(f"{len(payload['geo']['timezones'])} timezones mapped to "
          f"{len(set(payload['geo']['timezones'].values()))} countries")
    print(f"payload {len(body.encode()) / 1024:.0f} KB uncompressed")

    if payload["unmapped_countries"]:
        print(f"\n  WARN  no ISO country code for: "
              f"{', '.join(payload['unmapped_countries'])}")
        print("        visitors there will not be recognised by the "
              "'your country' section; add them to COUNTRY_CODES.")

    if args.check:
        print("\ncheck only: nothing written")
        return 0

    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    DATA_FILE.write_text(body + "\n")
    print(f"wrote {DATA_FILE.relative_to(HERE)}")

    if args.data_only:
        return 0

    written = build_dist(args.dist)
    total = sum(p.stat().st_size for p in written)
    print(f"\nbuilt {args.dist.relative_to(HERE) if args.dist.is_relative_to(HERE) else args.dist}/"
          f" — {len(written)} files, {total / 1024:.0f} KB")
    for path in written:
        print(f"  {path.relative_to(args.dist)}")
    print("\nDeploy that directory as-is. It contains no server, no database\n"
          "and no admin page.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
