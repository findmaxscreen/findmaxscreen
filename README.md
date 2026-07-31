# FindMaxScreen

A searchable list of every IMAX theatre, built to answer one question the
official channels won't: **which ones still run 15/70 mm film?**

Christopher Nolan and Denis Villeneuve have both done more than anyone to keep
film projection alive, and IMAX still offers no standing way to find the
theatres that run it — their own [theatre
finder](https://www.imax.com/theatre/finder) searches by location and nothing
else, and the theatre pages behind it never mention film, screen size or aspect
ratio. When a big title opens, IMAX posts a one-off list of participating
70 mm venues, and then it goes stale. This is the standing list.

**476 venues · 56 countries · 58 still running 15/70 mm film.**

## How it works

**[`ARCHITECTURE.md`](ARCHITECTURE.md)** has the diagrams and the reasoning;
what follows is the short version.

```
imax.fandom.com ──MediaWiki API──> sync.py ──> theatres.sqlite3 ──> export.py ──> dist/
                                                    ▲                          │
                                        geocode.py ─┘                    GitHub Pages
                                     (OpenStreetMap)
```

The published site is **static**: six files, **49 KB gzipped**, no server, no
database, no API. The whole dataset ships in one JSON and every search, filter
and sort happens in the browser. There is nothing to attack and nothing to keep
running.

| | |
|---|---|
| `sync.py` | Pulls the wiki through the MediaWiki API, keyed on revision id. Archives every revision, soft-deletes vanished venues, records every field-level change, and validates the result. |
| `geocode.py` | Adds coordinates from OpenStreetMap/Nominatim. ODbL, so they can be stored permanently. |
| `export.py` | Validates, writes `web/data/venues.json`, assembles `dist/` from an allow-list. |
| `serve.py` | Local only, loopback only. Serves the site plus a read-only API for the admin page. |

## Running it locally

```bash
python3 sync.py            # pull the wiki (no-op if the revision hasn't moved)
python3 geocode.py         # coordinates for anything new
python3 export.py          # validate and build dist/
python3 serve.py           # http://127.0.0.1:8787
```

The admin page — refresh, validation results, the full change log — is at
`/admin.html` on the local server. It is **never published**: `export.py` builds
`dist/` from an explicit allow-list and then re-scans the output, refusing if
anything matching `admin`, `sync.py`, `serve.py` or `.sqlite3` got in.

## Tests

```bash
python3 -m unittest discover -p 'test_*.py'   # parser, database, API, export
node --test test_query.mjs                    # browser-side search and filters
python3 test_bundle.py                        # assertions on the data being published
python3 smoke.py                              # loads the built site in a real browser
```

`smoke.py` exists because unit tests could not catch the bug that prompted it: a
refactor left a stale call site, the page threw *after* rendering its results,
and every one of 177 tests still passed while the pager silently vanished.

## Deployment

Live at **[findmaxscreen.com](https://findmaxscreen.com)**, published by
[`.github/workflows/daily.yml`](.github/workflows/daily.yml) to GitHub Pages,
daily. Most days the wiki revision hasn't moved and the run is a
single HTTP request.

Cloudflare's free CDN fronts the apex, holding the site at the edge for a month
and purging on each deploy — Pages hard-codes a ten-minute TTL, which on a
low-traffic site meant most visitors paid a ~250 ms origin round-trip. See
[REQUIREMENTS.md](REQUIREMENTS.md) for the measurements and the SSL trade-off.

`theatres.sqlite3` is committed to this repository deliberately. It holds the entire
audit trail — soft-deletes, `venue_changes`, first-seen dates — and every
geocode. A job starting from an empty database would lose that history *and*
re-geocode all 476 venues daily, which would get the project blocked by
Nominatim within a week.

## Data and licensing

Venue data comes from [List of IMAX venues](https://imax.fandom.com/wiki/List_of_IMAX_venues)
on the IMAX Wiki, written by its contributors and reused under
[CC BY-SA 3.0](https://creativecommons.org/licenses/by-sa/3.0/) per
[Fandom's licensing terms](https://www.fandom.com/licensing). Coordinates and
venue websites come from [OpenStreetMap](https://www.openstreetmap.org/copyright)
contributors under [ODbL](https://opendatacommons.org/licenses/odbl/).

This project is a derived work offered under the same licence. It is
independent, and not affiliated with or endorsed by IMAX Corporation or Fandom.

Notably, it does **not** scrape imax.com, which blocks automated access
including to its own `robots.txt`, nor a search engine to work around that.
Showtimes link out rather than being stored, because search results are licensed
for display, not for keeping.
