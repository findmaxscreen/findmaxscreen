#!/usr/bin/env python3
"""Sync the IMAX venue list from imax.fandom.com into a local SQLite database.

The source is a single wiki page of sortable tables.  Rather than scrape the
rendered HTML (Fandom answers plain fetches with HTTP 402 anyway), this reads
the raw wikitext through the MediaWiki API, which also hands back a revision id
- the natural change-detection key.  If the revision has not moved, the sync is
a no-op.

Every fetched revision is archived under snapshots/ before parsing, and venues
that disappear upstream are soft-deleted rather than dropped, so a bad wiki edit
can never destroy local history.

Usage:
    ./sync.py                 # fetch and apply if the revision moved
    ./sync.py --dry-run       # parse and report, write nothing
    ./sync.py --force         # re-parse the current revision
    ./sync.py --json          # machine-readable summary on stdout
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
import sys
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote_plus, urlencode
from urllib.request import Request, urlopen

HERE = Path(__file__).resolve().parent
DEFAULT_DB = HERE / "theatres.sqlite3"
SCHEMA = HERE / "schema.sql"
SNAPSHOT_DIR = HERE / "snapshots"
# Rows the wiki holds that this mirror refuses to carry. See load_suppressions.
SUPPRESSED = HERE / "suppressed.json"

API = "https://imax.fandom.com/api.php"
PAGE_TITLE = "List_of_IMAX_venues"
PAGE_URL = "https://imax.fandom.com/wiki/List_of_IMAX_venues"
USER_AGENT = "findmaxscreen/1.0 (personal offline mirror; python-urllib)"

MAPS_SEARCH = "https://www.google.com/maps/search/?api=1&query="

# A revision that parses to fewer than this fraction of the previous venue count
# is treated as upstream breakage (vandalism, a table restructure) and refused.
SHRINK_GUARD = 0.90

# Fields compared between revisions; a difference in any of them is recorded in
# venue_changes.  Provenance columns are deliberately excluded.
TRACKED_FIELDS = (
    "region", "country", "state", "city", "name", "maps_url",
    "screen_ar", "digital_projector", "digital_raw", "projector_family",
    "max_digital_ar", "film_projector", "film_raw", "film_family",
    "has_70mm", "is_dome", "is_1_43", "is_temporary", "commercial_films",
    "screen_w_m", "screen_h_m", "screen_area_m2", "dimensions_raw",
    "data_notes",
)


class ParseError(RuntimeError):
    pass


# --------------------------------------------------------------------------- #
# Fetching
# --------------------------------------------------------------------------- #

def fetch_revision(timeout: int = 30) -> dict:
    """Return {revid, timestamp, wikitext} for the current page revision."""
    query = urlencode({
        "action": "query",
        "prop": "revisions",
        "titles": PAGE_TITLE,
        "rvprop": "ids|timestamp|content",
        "rvslots": "main",
        "rvlimit": "1",
        "format": "json",
        "formatversion": "2",
    })
    req = Request(f"{API}?{query}", headers={
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
    })
    with urlopen(req, timeout=timeout) as resp:
        payload = json.load(resp)

    pages = payload.get("query", {}).get("pages") or []
    if not pages or pages[0].get("missing"):
        raise ParseError(f"page {PAGE_TITLE!r} not found via the API")
    rev = pages[0]["revisions"][0]
    return {
        "revid": int(rev["revid"]),
        "timestamp": rev["timestamp"],
        "wikitext": rev["slots"]["main"]["content"],
    }


# --------------------------------------------------------------------------- #
# Wikitext cleaning
# --------------------------------------------------------------------------- #

def clean(text: str) -> str:
    """Strip wiki and HTML markup from a cell, preserving line breaks."""
    if not text:
        return ""
    s = text
    s = re.sub(r"<ref[^>]*/>", "", s, flags=re.I)
    s = re.sub(r"<ref[^>]*>.*?</ref>", "", s, flags=re.I | re.S)
    s = re.sub(r"<sup>\s*2\s*</sup>", "²", s, flags=re.I)
    s = re.sub(r"<br\s*/?>", "\n", s, flags=re.I)
    s = re.sub(r"<[^>]+>", " ", s)
    s = re.sub(r"\{\{[^{}]*\}\}", "", s)
    # [[Target|Label]] -> Label, [[Target]] -> Target
    s = re.sub(r"\[\[[^\]|]*\|([^\]]*)\]\]", r"\1", s)
    s = re.sub(r"\[\[([^\]]*)\]\]", r"\1", s)
    s = re.sub(r"\[(?:https?|//)\S+\s+([^\]]*)\]", r"\1", s)
    s = s.replace("'''", "").replace("''", "")
    for entity, char in (("&nbsp;", " "), ("&#160;", " "), ("&amp;", "&"),
                         ("&ndash;", "–"), ("&mdash;", "—"),
                         ("&times;", "×"), ("&quot;", '"')):
        s = s.replace(entity, char)
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r" *\n *", "\n", s)
    return s.strip()


def clean_line(text: str) -> str:
    """Clean a cell that should end up as a single line."""
    return re.sub(r"\s*\n\s*", " ", clean(text)).strip()


# --------------------------------------------------------------------------- #
# Wikitable parsing
# --------------------------------------------------------------------------- #

# A cell may carry HTML attributes, separated from its content by a single pipe:
#   | rowspan="5" |Austria
_ATTR_PAIR = r'[A-Za-z][\w-]*\s*=\s*(?:"[^"]*"|\'[^\']*\'|[^\s|]+)'
_ATTRS_RE = re.compile(rf"^\s*((?:{_ATTR_PAIR})(?:\s+{_ATTR_PAIR})*)\s*\|(?!\|)(.*)$", re.S)
_ROWSPAN_RE = re.compile(r'rowspan\s*=\s*"?(\d+)"?', re.I)
_ROW_SPLIT_RE = re.compile(r"^\|-.*$", re.M)
_TABLE_RE = re.compile(r"^\{\|.*?^\|\}", re.M | re.S)
_SECTION_RE = re.compile(r"^==+\s*(.+?)\s*==+\s*$", re.M)


def _split_attrs(piece: str) -> tuple[str, str]:
    m = _ATTRS_RE.match(piece)
    if m:
        return m.group(1), m.group(2)
    return "", piece


def parse_cells(chunk: str, marker: str = "|") -> list[list]:
    """Parse one row of wikitext into [[content, rowspan], ...].

    Handles inline `||` separators and content that continues onto following
    lines (the screen-dimension cells wrap metric and imperial onto two lines).
    """
    cells: list[list] = []
    current: list | None = None
    for line in chunk.split("\n"):
        if line.startswith("|+") or line.startswith("|}") or line.startswith("{|"):
            current = None
            continue
        if line[:1] in ("|", "!"):
            sep = "!!" if line[0] == "!" else "||"
            for piece in line[1:].split(sep):
                attrs, content = _split_attrs(piece)
                span = _ROWSPAN_RE.search(attrs)
                cells.append([content, int(span.group(1)) if span else 1])
                current = cells[-1]
        elif current is not None and line.strip():
            current[0] += "\n" + line
    return cells


def parse_table(table: str, warnings: list[str] | None = None
                ) -> tuple[list[str], list[list[str]]]:
    """Return (header_texts, rows) with rowspans expanded into every row.

    A rowspan is a count the editor maintains by hand, and it drifts: r2940
    added a 23rd Canadian venue under `rowspan="22"`, so the last row (Kramer
    IMAX, Regina) fell out of the span and every cell slid one column left -
    its province "SK" became its country, and the export refused to build.

    The table's own shape gives the drift away. Every data row writes every
    cell, even empty ones, so a row with fewer cells than there are uncovered
    columns is sitting under a span that ended one row too early; a row with
    more is under one that runs one row too long. Both are repaired here - the
    span that ended on the previous row is extended, or the span on its last
    row is cut short - and each repair is reported through `warnings`, so the
    sync log names the row rather than leaving a phantom country to explain.

    A row can also be long for a reason no rowspan explains: a stray `|` on a
    line of its own before the first real cell. r2951 added Egypt that way, and
    every cell slid one column *right* - the city read as the country, the
    venue as the city - so validation refused the whole revision over one row.
    An empty leading cell in an over-long row carries nothing; it is dropped,
    and reported, before the surplus is blamed on a span.
    """
    lines = table.split("\n")
    header_idx = [i for i, ln in enumerate(lines) if ln.startswith("!")]
    headers = [clean_line(c[0]) for c in
               parse_cells("\n".join(lines[i] for i in header_idx))]
    if not headers:
        raise ParseError("table has no header row")

    # Drop the header block so it is not mistaken for a data row.
    body = "\n".join(lines[header_idx[-1] + 1:])

    ncols = len(headers)
    rows: list[list[str]] = []
    # col -> [content, rows still to cover, index of the last row it covered]
    pending: dict[int, list] = {}

    def describe(cells: list[list]) -> str:
        return " | ".join(clean_line(c[0]) for c in cells[:4])

    for chunk in _ROW_SPLIT_RE.split(body):
        cells = parse_cells(chunk)
        if not cells:
            # A row consisting only of carried-over cells is not a real venue.
            continue
        rowno = len(rows)
        active = {col: p for col, p in pending.items() if p[1] > 0}
        uncovered = ncols - len(active)

        while len(cells) > uncovered and not clean_line(cells[0][0]):
            # A row that is both over-long and opens with a blank is not
            # data: its first written cell is the country, which is required,
            # or a state under a country's span, in which case the row is not
            # over-long. Either way the blank is a stray separator.
            cells.pop(0)
            if warnings is not None:
                warnings.append(
                    f"stray empty cell before the first column; dropped, "
                    f"so the row reads: {describe(cells)}")

        if len(cells) < uncovered:
            # Short by k: the k leftmost spans that ended on the previous row
            # were undercounted. Extend each by this row.
            ended = sorted(col for col, p in pending.items()
                           if p[1] == 0 and p[2] == rowno - 1)
            for col in ended[:uncovered - len(cells)]:
                pending[col][1] = 1
                active[col] = pending[col]
                if warnings is not None:
                    warnings.append(
                        f"rowspan for {clean_line(pending[col][0])!r} "
                        f"(column {headers[col]!r}) ends one row early; "
                        f"extended to cover: {describe(cells)}")
        elif len(cells) > uncovered:
            # Long by k: the k leftmost spans on their final row were
            # overcounted. Release them so this row's own cells land.
            ending = sorted(col for col, p in active.items() if p[1] == 1)
            for col in ending[:len(cells) - uncovered]:
                pending[col][1] = 0
                del active[col]
                if warnings is not None:
                    warnings.append(
                        f"rowspan for {clean_line(pending[col][0])!r} "
                        f"(column {headers[col]!r}) runs one row long; "
                        f"cut before: {describe(cells)}")

        values: list[str] = []
        idx = 0
        for col in range(ncols):
            carried = active.get(col)
            if carried:
                values.append(carried[0])
                carried[1] -= 1
                carried[2] = rowno
            elif idx < len(cells):
                content, span = cells[idx]
                idx += 1
                # Recorded even for a single-row cell: a forgotten or
                # unbumped rowspan looks identical from the row below.
                pending[col] = [content, span - 1, rowno]
                values.append(content)
            else:
                values.append("")
        rows.append(values)
    return headers, rows


COLUMN_MAP = {
    "country": "country",
    "state": "state",
    "province": "state",
    "state/province": "state",
    "city": "city",
    "location name": "name",
    "location": "name",
    "name": "name",
    "screen aspect ratio (ar)": "screen_ar",
    "screen aspect ratio": "screen_ar",
    "digital projector": "digital_raw",
    "maximum ar for digital projection": "max_digital_ar",
    "film projector": "film_raw",
    "screen dimensions": "dimensions_raw",
    "commercial films shown?": "commercial_films",
    "commercial films shown": "commercial_films",
}
REQUIRED_COLUMNS = ("country", "city", "name")


def map_columns(headers: list[str]) -> tuple[dict[int, str], list[str]]:
    mapping: dict[int, str] = {}
    unknown: list[str] = []
    for i, h in enumerate(headers):
        key = re.sub(r"\s+", " ", h.strip().lower())
        field = COLUMN_MAP.get(key)
        if field:
            mapping[i] = field
        elif key:
            unknown.append(h)
    missing = [c for c in REQUIRED_COLUMNS if c not in mapping.values()]
    if missing:
        raise ParseError(f"table is missing required column(s): {', '.join(missing)}")
    return mapping, unknown


# --------------------------------------------------------------------------- #
# Field normalization
# --------------------------------------------------------------------------- #

# Ordered: the first pattern that matches wins, so more specific forms come
# first ("GT Laser" before the bare "Laser", "for Dome" before "Digital").
DIGITAL_FAMILIES = (
    (re.compile(r"gt\s*laser", re.I), "GT Laser", "IMAX GT Laser"),
    (re.compile(r"co\s*la", re.I), "CoLa", "IMAX CoLa"),
    (re.compile(r"\bxt\b", re.I), "XT", "IMAX Laser XT"),
    (re.compile(r"dome", re.I), "Dome Laser", "IMAX Dome Laser"),
    (re.compile(r"digital", re.I), "Digital", "IMAX Digital"),
    (re.compile(r"laser", re.I), "Laser", "IMAX with Laser"),
)

FILM_FAMILIES = (
    (re.compile(r"gt\s*3\s*d", re.I), "GT3D", "IMAX GT3D 15/70 mm"),
    (re.compile(r"gt\s*dome", re.I), "GT Dome", "IMAX GT Dome 15/70 mm"),
    (re.compile(r"sr\s*dome", re.I), "SR Dome", "IMAX SR Dome 15/70 mm"),
    (re.compile(r"\bsr\b", re.I), "SR", "IMAX SR 15/70 mm"),
    (re.compile(r"\bgt\b", re.I), "GT", "IMAX GT 15/70 mm"),
    (re.compile(r"dome", re.I), "Dome", "IMAX Dome 15/70 mm"),
)

_ANNOTATION_RE = re.compile(r"\(([^)]*)\)")
_FILM_15_70_RE = re.compile(r"15\s*/\s*70")
_AR_TYPO_RE = re.compile(r"\b(\d):(\d{2}):(\d)\b")
_AR_1_43_RE = re.compile(r"\b1\.43\b")


def _normalize_ar(raw: str) -> str:
    """Repair aspect ratios the wiki mistypes, leaving real information intact.

    Three Vietnamese rows write "1:90:1" with colons where the first separator
    should be a decimal point.  That is a typo, and it hid those venues from the
    1.90 filter entirely.  "Dome 1.43:1" is *not* a typo - the prefix is real
    information - so it survives untouched and `is_dome` carries it.
    """
    return _AR_TYPO_RE.sub(r"\1.\2:\3", raw)


def _classify(value: str, families) -> tuple[str, str]:
    for pattern, family, display in families:
        if pattern.search(value):
            return family, display
    return "", ""


def _normalize_numbers(text: str) -> str:
    """Resolve the two jobs a comma does in these cells.

    German rows write the decimal separator as a comma ("203,8 m²") while
    imperial figures use it for thousands ("2,194 sq ft").  Three digits after a
    comma means a thousands separator; one or two means a decimal point.  Getting
    this wrong inflates an area by 10x, which is exactly what a "biggest screen"
    sort surfaces first.
    """
    text = re.sub(r"(\d),(\d{3})(?!\d)", r"\1\2", text)
    return re.sub(r"(\d),(\d{1,2})(?!\d)", r"\1.\2", text)


def _parse_dimensions(raw: str) -> tuple[float | None, float | None, float | None]:
    """Pull the metric width/height/area out of a screen-dimensions cell.

    Cells mix metric and imperial across lines and vary in spacing, e.g.
    "17.4 m x 9.2 m\n57.1 ft x 30.2 ft" or "19.10m×10.60m".  Dome rows may give
    a single diameter, which lands in width.
    """
    if not raw:
        return None, None, None
    text = _normalize_numbers(raw)
    pair = re.search(r"(\d+(?:\.\d+)?)\s*m\s*[×x✕*]\s*(\d+(?:\.\d+)?)\s*m\b", text, re.I)
    width = height = None
    if pair:
        width, height = float(pair.group(1)), float(pair.group(2))
    else:
        single = re.search(r"(\d+(?:\.\d+)?)\s*m\b(?!²)", text, re.I)
        if single:
            width = float(single.group(1))
    # A couple of rows are written "21.0 m × 0 m", i.e. the height is unknown
    # rather than zero.  Storing 0.0 would render as a "21 × 0 m" screen.
    width = width or None
    height = height or None
    area_m = re.search(r"(\d+(?:\.\d+)?)\s*m²", text)
    area = float(area_m.group(1)) if area_m else None
    if area is None and width and height:
        area = round(width * height, 1)
    return width, height, area or None


def _yes_no(value: str) -> int | None:
    v = value.strip().lower()
    if v.startswith("yes"):
        return 1
    if v.startswith("no"):
        return 0
    return None


def _slug(*parts: str) -> str:
    joined = "|".join(p.strip().lower() for p in parts)
    decomposed = unicodedata.normalize("NFKD", joined)
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9|]+", "", stripped)


def build_record(region: str, fields: dict[str, str]) -> dict:
    """Turn one raw parsed row into a normalized venue record."""
    notes: list[str] = []

    country = clean_line(fields.get("country", ""))
    state = clean_line(fields.get("state", ""))
    city = clean_line(fields.get("city", ""))
    name = clean_line(fields.get("name", ""))
    screen_ar = _normalize_ar(clean_line(fields.get("screen_ar", "")))
    max_digital_ar = clean_line(fields.get("max_digital_ar", ""))
    digital_raw = clean_line(fields.get("digital_raw", ""))
    film_raw = clean_line(fields.get("film_raw", ""))
    dimensions_raw = clean(fields.get("dimensions_raw", ""))
    commercial = _yes_no(clean_line(fields.get("commercial_films", "")))

    has_70mm = bool(_FILM_15_70_RE.search(film_raw))

    # A handful of rows carry a digital projector value in the film column.
    # Route it to the digital side rather than inventing a film projector.
    if film_raw and not has_70mm and _classify(film_raw, DIGITAL_FAMILIES)[0]:
        notes.append(f'film column held a digital projector value ("{film_raw}")')
        if not digital_raw:
            digital_raw = film_raw
        film_raw_for_family = ""
    else:
        film_raw_for_family = film_raw

    digital_family, digital_display = _classify(digital_raw, DIGITAL_FAMILIES)
    film_family, film_display = ("", "")
    if film_raw_for_family:
        film_family, film_display = _classify(film_raw_for_family, FILM_FAMILIES)
        if not film_family and has_70mm:
            film_family, film_display = "15/70", "IMAX 15/70 mm"

    # Inline annotations such as "(2D Only)" or "(Temporary)".
    annotations = _ANNOTATION_RE.findall(f"{digital_raw} {film_raw}")
    for note in annotations:
        note = note.strip()
        if note:
            notes.append(note)
    is_temporary = any("temporar" in a.lower() for a in annotations)

    width, height, area = _parse_dimensions(dimensions_raw)
    # A stated area that disagrees with width x height means either the wiki row
    # is inconsistent or this parser mis-read it.  Flag rather than silently pick
    # a side, so a bad size never sorts to the top unexplained.
    if width and height and area and abs(area - width * height) > 0.2 * area:
        notes.append(f"stated area {area} m² disagrees with {width} × {height} m")

    combined = f"{digital_raw} {film_raw}".lower()

    return {
        "venue_key": _slug(country, state, city, name),
        "region": region,
        "country": country,
        "state": state,
        "city": city,
        "name": name,
        "maps_url": MAPS_SEARCH + quote_plus(", ".join(p for p in (name, city, country) if p)),
        "screen_ar": screen_ar,
        "digital_projector": digital_display,
        "digital_raw": digital_raw,
        "projector_family": digital_family,
        "max_digital_ar": max_digital_ar,
        "film_projector": film_display,
        "film_raw": film_raw,
        "film_family": film_family,
        "has_70mm": int(has_70mm),
        "is_dome": int("dome" in combined),
        # Dome venues are written "Dome 1.43:1", so anchoring at the start of the
        # string silently excluded all 28 of them from the 1.43 filter.  The flag
        # means "the screen is 1.43:1"; use is_dome to separate flat from dome.
        "is_1_43": int(bool(_AR_1_43_RE.search(screen_ar))),
        "is_temporary": int(is_temporary),
        "commercial_films": commercial,
        "screen_w_m": width,
        "screen_h_m": height,
        "screen_area_m2": area,
        "dimensions_raw": dimensions_raw,
        "data_notes": "; ".join(notes),
    }


def load_suppressions(path: Path = SUPPRESSED) -> dict[str, str]:
    """Return {venue_key: reason} for rows the mirror should not carry.

    The site mirrors the wiki, and the wiki is edited by anyone. r2950 added
    four African venues in one sitting - "Nairocinema & IMAX" in Casablanca,
    "IMAX Cairojector", a 1.90:2 screen - none of which exists. The right fix
    is upstream, but until it lands the mirror would publish them. This file
    names the rows to hold back, each with the reason and the revision that
    brought it, so the list reads as a record rather than a blocklist.

    An entry is a temporary measure. parse_page warns when one matches no
    row any more, which is the cue that upstream has fixed it and the entry
    can go.
    """
    if not path.is_file():
        return {}
    entries = json.loads(path.read_text())
    if not isinstance(entries, list):
        raise ParseError(f"{path.name}: expected a list of entries")
    suppressed: dict[str, str] = {}
    for entry in entries:
        key, reason = entry.get("venue_key", ""), entry.get("reason", "")
        if not key or not reason:
            raise ParseError(f"{path.name}: every entry needs a venue_key and a reason")
        suppressed[key] = reason
    return suppressed


def parse_page(wikitext: str, suppressed: dict[str, str] | None = None
               ) -> tuple[list[dict], dict[str, int], list[str]]:
    """Parse the whole page into venue records, per-region counts and warnings.

    `suppressed` maps venue_key to a reason; a row whose key is listed is
    dropped and reported. See load_suppressions.
    """
    warnings: list[str] = []
    records: list[dict] = []
    per_region: dict[str, int] = {}
    suppressed = dict(suppressed or {})
    unmatched = set(suppressed)

    parts = _SECTION_RE.split(wikitext)
    if len(parts) < 3:
        raise ParseError("no == section == headings found; page structure changed")

    for i in range(1, len(parts), 2):
        # A heading is wikitext like any cell, so it gets the same cleaning.
        # Every field in a row already went through clean_line() while the
        # region - the one value that comes from the heading rather than the
        # table - was taken raw. That held until r2938 wrote
        # "== Europe<ref>IMAX CoLa</ref> ==" and 94 venues landed in a region
        # literally named `Europe<ref>IMAX CoLa</ref>`, which nothing caught
        # until the unknown_region check refused the export.
        region, body = clean_line(parts[i]), parts[i + 1]
        table_match = _TABLE_RE.search(body)
        if not table_match:
            warnings.append(f"section {region!r} has no table; skipped")
            continue
        headers, rows = parse_table(table_match.group(0), warnings)
        mapping, unknown = map_columns(headers)
        for col in unknown:
            warnings.append(f"section {region!r}: unmapped column {col!r}")

        kept = 0
        for row in rows:
            fields = {field: row[i] for i, field in mapping.items()}
            record = build_record(region, fields)
            if not record["name"] or not record["venue_key"]:
                continue
            if record["venue_key"] in suppressed:
                unmatched.discard(record["venue_key"])
                warnings.append(
                    f"suppressed {record['name']!r} ({record['venue_key']}): "
                    f"{suppressed[record['venue_key']]}")
                continue
            records.append(record)
            kept += 1
        per_region[region] = kept

    # Guarantee unique keys; a genuine duplicate is a wiki problem, not a reason
    # to abort the whole sync.
    seen: dict[str, int] = {}
    for record in records:
        key = record["venue_key"]
        seen[key] = seen.get(key, 0) + 1
        if seen[key] > 1:
            record["venue_key"] = f"{key}#{seen[key]}"
            note = f"duplicate of another row with the same country/state/city/name"
            record["data_notes"] = "; ".join(filter(None, (record["data_notes"], note)))
            warnings.append(f"duplicate venue key {key!r} ({record['name']})")

    for key in sorted(unmatched):
        warnings.append(
            f"suppression for {key!r} matched no row; if upstream has fixed "
            f"it, remove the entry from {SUPPRESSED.name}")

    return records, per_region, warnings


# --------------------------------------------------------------------------- #
# Database
# --------------------------------------------------------------------------- #

# Columns added after the first databases were created.  CREATE TABLE IF NOT
# EXISTS will not add them to an existing file, so they are applied by hand.
MIGRATIONS = (
    ("lat", "REAL"),
    ("lon", "REAL"),
    ("geo_source", "TEXT NOT NULL DEFAULT ''"),
    ("geo_precision", "TEXT NOT NULL DEFAULT ''"),
    ("geocoded_at", "TEXT"),
    ("website", "TEXT NOT NULL DEFAULT ''"),
    ("geo_matched", "TEXT NOT NULL DEFAULT ''"),
)


def migrate(conn: sqlite3.Connection) -> list[str]:
    """Add any columns an older database predates.  Returns what was added."""
    have = {row["name"] for row in conn.execute("PRAGMA table_info(venues)")}
    added = []
    for column, decl in MIGRATIONS:
        if column not in have:
            conn.execute(f"ALTER TABLE venues ADD COLUMN {column} {decl}")
            added.append(column)
    if added:
        conn.commit()
    return added


def connect(db_path: Path) -> sqlite3.Connection:
    fresh = not db_path.exists()
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA.read_text())
    migrate(conn)
    if fresh:
        conn.commit()
    return conn


def latest_revision(conn: sqlite3.Connection) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM revisions ORDER BY fetched_at DESC, revid DESC LIMIT 1"
    ).fetchone()


def _normalize_for_compare(value):
    if isinstance(value, float):
        return round(value, 4)
    return value


def apply_revision(conn: sqlite3.Connection, records: list[dict], revid: int,
                   rev_timestamp: str, fetched_at: str, sha: str,
                   snapshot: str) -> dict:
    added = removed = changed = 0

    with conn:
        existing = {
            row["venue_key"]: row
            for row in conn.execute("SELECT * FROM venues")
        }

        for record in records:
            row = existing.get(record["venue_key"])
            if row is None:
                columns = list(record) + [
                    "first_seen_revid", "last_seen_revid",
                    "first_seen_at", "last_seen_at",
                ]
                values = [record[c] for c in record] + [revid, revid, fetched_at, fetched_at]
                conn.execute(
                    f"INSERT INTO venues ({', '.join(columns)}) "
                    f"VALUES ({', '.join('?' * len(columns))})",
                    values,
                )
                added += 1
                continue

            diffs = [
                (field, row[field], record[field])
                for field in TRACKED_FIELDS
                if _normalize_for_compare(row[field]) != _normalize_for_compare(record[field])
            ]
            if row["removed_at"] is not None:
                diffs.append(("_status", "removed", "present"))
            for field, old, new in diffs:
                conn.execute(
                    "INSERT INTO venue_changes (venue_id, revid, field, old_value,"
                    " new_value, changed_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (row["id"], revid, field,
                     None if old is None else str(old),
                     None if new is None else str(new), fetched_at),
                )
            assignments = ", ".join(f"{f} = ?" for f in TRACKED_FIELDS)
            conn.execute(
                f"UPDATE venues SET {assignments}, last_seen_revid = ?,"
                f" last_seen_at = ?, removed_at = NULL WHERE id = ?",
                [record[f] for f in TRACKED_FIELDS] + [revid, fetched_at, row["id"]],
            )
            if diffs:
                changed += 1

        # Soft-delete anything the current revision no longer lists.
        gone = conn.execute(
            "SELECT id, name, city, country FROM venues"
            " WHERE last_seen_revid IS NOT ? AND removed_at IS NULL", (revid,)
        ).fetchall()
        for row in gone:
            conn.execute(
                "INSERT INTO venue_changes (venue_id, revid, field, old_value,"
                " new_value, changed_at) VALUES (?, ?, '_status', 'present',"
                " 'removed', ?)", (row["id"], revid, fetched_at),
            )
            conn.execute("UPDATE venues SET removed_at = ? WHERE id = ?",
                         (fetched_at, row["id"]))
        removed = len(gone)

        conn.execute(
            "INSERT INTO revisions (revid, wiki_timestamp, fetched_at,"
            " wikitext_sha256, snapshot_path, venue_count, n_added, n_removed,"
            " n_changed) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(revid) DO UPDATE SET fetched_at = excluded.fetched_at,"
            " venue_count = excluded.venue_count, n_added = excluded.n_added,"
            " n_removed = excluded.n_removed, n_changed = excluded.n_changed,"
            " snapshot_path = excluded.snapshot_path",
            (revid, rev_timestamp, fetched_at, sha, snapshot, len(records),
             added, removed, changed),
        )

    return {
        "added": added, "removed": removed, "changed": changed,
        "venues": len(records),
        "removed_names": [f"{r['name']} ({r['city']}, {r['country']})" for r in gone],
    }


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #

# Screens outside these bounds are a parse failure, not a remarkable cinema:
# the smallest real IMAX is around 100 m², the largest around 800 m².
PLAUSIBLE_W_M = (5, 120)
PLAUSIBLE_H_M = (4, 45)
PLAUSIBLE_AREA_M2 = (50, 1500)

KNOWN_REGIONS = ("Europe", "Asia", "Oceania", "Africa", "Americas")

# (severity, name, explanation, sql) - each query returns the offending rows.
#
# ERROR means the database itself is wrong and the sync should be treated as
# failed.  WARN means the upstream wiki is inconsistent: worth surfacing, but
# not something this program can or should fix.  The flag checks re-derive each
# boolean straight from the text it came from, so a filter that silently stops
# matching - the 1.43-excludes-domes bug - fails here instead of in the UI.
CHECKS = (
    ("ERROR", "duplicate_venue_key",
     "venue_key must be unique; a collision means two venues share history",
     "SELECT venue_key AS detail FROM venues GROUP BY venue_key"
     " HAVING count(*) > 1"),

    ("ERROR", "missing_required_field",
     "every venue needs a key, name, country, city and maps link",
     "SELECT name AS detail FROM venues WHERE venue_key = '' OR name = ''"
     " OR country = '' OR city = '' OR maps_url = ''"),

    ("ERROR", "bad_commercial_films",
     "commercial_films must be 1, 0 or NULL",
     "SELECT name AS detail FROM venues WHERE commercial_films NOT IN (0, 1)"),

    ("ERROR", "unknown_region",
     f"region must be one of {', '.join(KNOWN_REGIONS)}",
     "SELECT DISTINCT region AS detail FROM venues WHERE region NOT IN"
     f" ({', '.join('?' * len(KNOWN_REGIONS))})"),

    ("WARN", "film70_flag_disagrees",
     "has_70mm should be set exactly when the film column mentions 15/70",
     "SELECT name AS detail FROM venues"
     " WHERE has_70mm != (replace(film_raw, ' ', '') LIKE '%15/70%')"),

    ("WARN", "film70_without_family",
     "a 15/70 venue whose projector model could not be classified",
     "SELECT name AS detail FROM venues WHERE has_70mm = 1 AND film_family = ''"),

    ("WARN", "ar_1_43_flag_disagrees",
     "is_1_43 should be set exactly when the aspect ratio mentions 1.43",
     "SELECT name AS detail FROM venues"
     " WHERE is_1_43 != (screen_ar LIKE '%1.43%')"),

    ("WARN", "dome_flag_disagrees",
     "is_dome should be set exactly when a projector string mentions a dome",
     "SELECT name AS detail FROM venues WHERE is_dome !="
     " (lower(digital_raw || ' ' || film_raw) LIKE '%dome%')"),

    ("WARN", "implausible_dimensions",
     "a screen measurement outside the range any real IMAX occupies",
     "SELECT name || ' (' || coalesce(screen_w_m, 0) || ' x '"
     " || coalesce(screen_h_m, 0) || ' m, ' || coalesce(screen_area_m2, 0)"
     " || ' m2)' AS detail FROM venues WHERE"
     f" (screen_w_m IS NOT NULL AND screen_w_m NOT BETWEEN {PLAUSIBLE_W_M[0]} AND {PLAUSIBLE_W_M[1]})"
     f" OR (screen_h_m IS NOT NULL AND screen_h_m NOT BETWEEN {PLAUSIBLE_H_M[0]} AND {PLAUSIBLE_H_M[1]})"
     f" OR (screen_area_m2 IS NOT NULL AND screen_area_m2 NOT BETWEEN {PLAUSIBLE_AREA_M2[0]} AND {PLAUSIBLE_AREA_M2[1]})"),

    ("ERROR", "geocode_inconsistent",
     "a venue marked as geocoded must have coordinates, and vice versa",
     "SELECT name AS detail FROM venues WHERE"
     " (geo_precision IN ('venue', 'city') AND (lat IS NULL OR lon IS NULL))"
     " OR (lat IS NOT NULL AND geo_precision = '')"),

    ("WARN", "geocode_unaudited",
     "a located venue should record what OpenStreetMap matched, so the point"
     " can be checked later without re-querying",
     "SELECT name AS detail FROM venues WHERE geo_precision IN ('venue', 'city')"
     " AND geo_matched = ''"),

    ("ERROR", "coordinates_out_of_range",
     "latitude must be within ±90 and longitude within ±180",
     "SELECT name AS detail FROM venues WHERE lat IS NOT NULL AND"
     " (lat NOT BETWEEN -90 AND 90 OR lon NOT BETWEEN -180 AND 180)"),

    ("WARN", "area_disagrees_with_dimensions",
     "the wiki's stated area does not match its own width x height",
     "SELECT name AS detail FROM venues WHERE data_notes LIKE '%disagrees%'"),
)


def validate(conn: sqlite3.Connection) -> list[dict]:
    """Check the database against its own invariants.

    Returns one finding per failed check, each with up to a few example rows.
    An empty list means everything reconciles.
    """
    findings: list[dict] = []

    # The FTS index is maintained by triggers; if one is ever dropped, search
    # quietly returns fewer venues than the table holds and nothing else gives
    # it away.  Counting venues_fts itself cannot detect that - it is an
    # external-content table, so count(*) reads straight through to `venues` and
    # always agrees.  The docsize shadow table is the actual index, so compare
    # against that.  (FTS5 writes it whenever columnsize is left at its default.)
    venues = conn.execute("SELECT count(*) FROM venues").fetchone()[0]
    try:
        indexed = conn.execute("SELECT count(*) FROM venues_fts_docsize").fetchone()[0]
    except sqlite3.OperationalError:
        indexed = venues  # no docsize table; nothing to compare against
    if venues != indexed:
        findings.append({
            "severity": "ERROR", "check": "fts_index_drift",
            "explanation": "the search index has drifted from the venues table",
            "count": abs(venues - indexed),
            "examples": [f"{venues} venues, {indexed} indexed"],
        })

    for severity, name, explanation, sql in CHECKS:
        params = KNOWN_REGIONS if name == "unknown_region" else ()
        rows = conn.execute(sql, params).fetchall()
        if rows:
            findings.append({
                "severity": severity, "check": name, "explanation": explanation,
                "count": len(rows),
                "examples": [str(r["detail"]) for r in rows[:5]],
            })
    return findings


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--dry-run", action="store_true",
                        help="parse and report; write nothing")
    parser.add_argument("--force", action="store_true",
                        help="re-parse even if the revision has not changed")
    parser.add_argument("--allow-shrink", action="store_true",
                        help="apply a revision that lost more than 10%% of venues")
    parser.add_argument("--from-file", type=Path,
                        help="parse a saved snapshot instead of fetching")
    parser.add_argument("--json", action="store_true",
                        help="emit a machine-readable summary")
    parser.add_argument("--validate-only", action="store_true",
                        help="check the existing database and exit; fetch nothing")
    parser.add_argument("--strict", action="store_true",
                        help="treat validation warnings as failures too")
    args = parser.parse_args(argv)

    out: list[str] = []

    def say(line: str = "") -> None:
        out.append(line)
        if not args.json:
            print(line, flush=True)

    def report(conn: sqlite3.Connection) -> tuple[list[dict], int]:
        """Print validation findings and return them with an exit code."""
        findings = validate(conn)
        errors = sum(1 for f in findings if f["severity"] == "ERROR")
        warnings_ = len(findings) - errors
        say()
        say(f"validation: {errors} error(s), {warnings_} warning(s)"
            if findings else "validation: all checks passed")
        for finding in findings:
            say(f"  {finding['severity']:<5} {finding['check']} "
                f"({finding['count']}) - {finding['explanation']}")
            for example in finding["examples"]:
                say(f"          · {example}")
        code = 1 if errors or (args.strict and findings) else 0
        return findings, code

    if args.validate_only:
        conn = connect(args.db)
        findings, code = report(conn)
        if args.json:
            print(json.dumps({"status": "validated", "findings": findings,
                              "log": out}))
        return code

    if args.from_file:
        wikitext = args.from_file.read_text()
        rev = {"revid": 0, "timestamp": "local", "wikitext": wikitext}
        say(f"parsing {args.from_file}")
    else:
        say(f"fetching {PAGE_URL}")
        rev = fetch_revision()
        say(f"revision {rev['revid']} (edited {rev['timestamp']}), "
            f"{len(rev['wikitext']):,} bytes of wikitext")

    conn = connect(args.db)
    previous = latest_revision(conn)

    if previous and previous["revid"] == rev["revid"] and not args.force and not args.dry_run:
        say(f"already at revision {rev['revid']}, nothing to do")
        summary = {"status": "current", "revid": rev["revid"],
                   "venues": previous["venue_count"], "added": 0,
                   "removed": 0, "changed": 0, "log": out}
        if args.json:
            print(json.dumps(summary))
        return 0

    sha = hashlib.sha256(rev["wikitext"].encode()).hexdigest()
    snapshot = ""
    if not args.dry_run and not args.from_file:
        # The archive belongs beside the database it describes, not beside this
        # script. A sync pointed at a scratch copy - which is how the guard
        # tests exercise the endpoint - otherwise writes its snapshot into the
        # repository's own snapshots/, leaving a file that belongs to no
        # revision for the daily job to sweep into a commit.
        snapshot_dir = args.db.resolve().parent / SNAPSHOT_DIR.name
        snapshot_dir.mkdir(exist_ok=True)
        stamp = re.sub(r"[^0-9A-Za-z]", "", rev["timestamp"])
        path = snapshot_dir / f"{stamp}-r{rev['revid']}.wiki"
        path.write_text(rev["wikitext"])
        snapshot = str(path.relative_to(snapshot_dir.parent))
        say(f"archived snapshot -> {snapshot}")

    records, per_region, warnings = parse_page(rev["wikitext"], load_suppressions())

    say()
    for region, count in per_region.items():
        say(f"  {region:<10} {count:>4} venues")
    say(f"  {'TOTAL':<10} {len(records):>4} venues "
        f"({sum(r['has_70mm'] for r in records)} with 15/70 mm film, "
        f"{sum(r['is_dome'] for r in records)} dome)")

    if args.dry_run:
        say()
        families: dict[str, int] = {}
        for record in records:
            key = f"{record['projector_family'] or '-'} / {record['film_family'] or '-'}"
            families[key] = families.get(key, 0) + 1
        say("  digital family / film family:")
        for key, count in sorted(families.items(), key=lambda kv: -kv[1]):
            say(f"    {key:<24} {count:>4}")

    for warning in warnings:
        say(f"  warning: {warning}")

    if previous and previous["venue_count"]:
        floor = previous["venue_count"] * SHRINK_GUARD
        if len(records) < floor and not args.allow_shrink:
            say()
            say(f"REFUSING: parsed {len(records)} venues, down from "
                f"{previous['venue_count']} (floor {floor:.0f}). The page may have"
                f" been restructured or vandalised. Nothing was written.")
            say("Inspect the snapshot, then re-run with --allow-shrink to accept.")
            if args.json:
                print(json.dumps({"status": "refused", "venues": len(records),
                                  "previous": previous["venue_count"], "log": out}))
            return 2

    if args.dry_run:
        say()
        say("dry run: nothing written")
        if args.json:
            print(json.dumps({"status": "dry-run", "venues": len(records), "log": out}))
        return 0

    result = apply_revision(conn, records, rev["revid"], rev["timestamp"],
                            datetime.now(timezone.utc).isoformat(timespec="seconds"),
                            sha, snapshot)
    say()
    say(f"+{result['added']} added · {result['removed']} removed · "
        f"{result['changed']} changed")
    for name in result["removed_names"][:20]:
        say(f"  removed: {name}")

    # Every update validates itself; a write that leaves the database
    # inconsistent should say so rather than wait to be noticed in the UI.
    findings, code = report(conn)

    if args.json:
        print(json.dumps({"status": "applied", "revid": rev["revid"],
                          "wiki_timestamp": rev["timestamp"], **result,
                          "findings": findings, "log": out}))
    return code


if __name__ == "__main__":
    sys.exit(main())
