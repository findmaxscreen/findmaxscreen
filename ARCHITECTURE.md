# Architecture

How FindMaxScreen is put together, and why. `REQUIREMENTS.md` carries the full
brief and the reasoning behind individual decisions; `HANDOFF.md` has the current
state and what is left to do.

Diagrams are SVG committed alongside this file, so they render wherever the
Markdown is read and never depend on anything outside the repository.

---

## 1. Data flow

![Data flow: the wiki and OpenStreetMap feed sync.py and geocode.py, which write theatres.sqlite3; export.py validates that database and builds dist/, which GitHub Actions publishes to GitHub Pages at findmaxscreen.com. serve.py and admin.html read the database but are never published.](docs/diagrams/data-flow.svg)

Two sources feed one database, and one script turns that database into a
directory of files. Nothing runs in production.

**The wiki is the source of truth for venue facts.** `sync.py` reads the raw
wikitext through the MediaWiki API rather than scraping the rendered page —
Fandom answers plain fetches with HTTP 402 anyway — and the API hands back a
revision id, which is the natural change-detection key. If the revision has not
moved, the sync stops. Most days that is the entire run.

**OpenStreetMap supplies what the wiki lacks.** The wiki has no coordinates, so
`geocode.py` fetches them from Nominatim. OSM rather than Google is a licensing
decision, not a technical one: Google's terms cap caching at 30 days and tie
indefinite storage of coordinates to their own map surface, whereas OSM data is
ODbL and may be stored permanently with attribution. Baking coordinates into a
static file and serving them forever is the whole point, so ODbL is the only
licence that permits the design.

**`theatres.sqlite3` is the history, not a cache.** Venues that vanish upstream
are soft-deleted rather than dropped, every field-level edit is recorded in
`venue_changes`, and each fetched revision is archived under `snapshots/`. That
is why the database is committed to the repository: a run starting from an empty
one would lose the audit trail *and* re-geocode all 476 venues daily, which
would get the project blocked by Nominatim within a week.

**`export.py` is the only path to the public site.** It validates the database,
refuses to build if anything is wrong, writes one JSON document, and copies an
explicit allow-list of files into `dist/`.

**The admin tooling never leaves the machine.** `serve.py` binds to loopback and
`admin.html` is not in the allow-list — so it is not that the admin page is
hidden in production, it is that it was never copied there. `build_dist()`
re-scans its own output and raises if anything matching `admin`, `sync.py`,
`serve.py` or `.sqlite3` got in.

### Why static

The published site is seven files, **49 KB gzipped**. The entire dataset ships in
one JSON and every search, filter, sort and page happens in the browser. For 476
rows that is both simpler and faster than any backend, and it has three
consequences worth stating plainly:

- **Nothing to attack.** No server, no database, no API, no secrets. The one
  state-changing endpoint in the project — `POST /api/sync`, which shells out —
  exists only on the loopback server and is never published.
- **Nothing to keep running.** A failed deploy leaves the previous one serving.
- **Nothing leaks.** The page makes exactly one request, for its own data file.
  The visitor's country is guessed from the browser's IANA timezone, so no IP is
  sent anywhere and no permission prompt appears. Outbound links fire only when
  a visitor clicks one.

---

## 2. The deploy gate

![The deploy gate: six sequential stages — unit tests, the sync shrink guard, database validation, the export allow-list, bundle data assertions, and a headless-browser smoke test — feeding a decision. All green publishes and commits the database; any failure stops and emails, leaving the previous deployment live.](docs/diagrams/deploy-gate.svg)

The job runs daily and nobody watches it, so the gate has to be trustworthy in
both directions: it must stop bad data, and it must not cry wolf on good data.
Each stage answers a different question.

| Stage | Question it answers |
|---|---|
| 1 · unit tests | Is the code correct? |
| 2 · shrink guard | Was the wiki vandalised or restructured? |
| 3 · `validate()` | Is the database internally coherent? |
| 4 · export allow-list | Does the bundle contain only public files? |
| 5 · `test_bundle.py` | Do the published numbers agree with each other? |
| 6 · `smoke.py` | Does the page actually render? |

Stages 2 and 3 are the ones that catch the outside world changing under us.
Stages 4 to 6 catch us changing something under ourselves.

**Stage 6 earns its place by having caught what nothing else could.** A refactor
changed a function's signature and left one call site passing a stale variable.
It threw *after* the venue cards had rendered, so the page looked healthy while
the pager silently never appeared — and all 160 unit tests passed throughout.
Unit tests on an extracted function say nothing about its call site. That gate is
verified by fault injection: re-introducing the exact bug makes it fail four
checks and exit non-zero.

Two properties of the gate that took a mistake each to learn:

- **A flaky gate is worse than no gate**, because it teaches you to re-run
  instead of read. The smoke test originally matched any log line containing
  `ERROR`, which swept in Chrome's own internal chatter and failed roughly one
  run in eight on a perfectly good build. It now requires the `:CONSOLE:` tag,
  so only page-level errors count.
- **A gate that fails on correct data gets ignored.** The coordinate audit flags
  venues whose OSM match shares no word with the venue name — but "Cinema City
  Zakopianka" legitimately sits on ulica Zakopiańska. It now folds diacritics,
  allows a shared prefix, exempts CJK names, and asserts a *rate* rather than
  zero.

---

## 3. Where the data comes from, and what it is worth

| Field group | Source | Overwritten by a sync? |
|---|---|---|
| Name, city, country, projector, screen, aspect ratio | IMAX Wiki | **Yes** — every run |
| `lat`, `lon`, `geo_precision`, `geo_matched` | OpenStreetMap | No |
| `website` | OpenStreetMap tags | No |
| `first_seen`, `removed_at`, `venue_changes` | Derived locally | No |

That split decides where a correction belongs, and it is not obvious from the
outside — which is why the site's footer names the wiki first. A fix made there
reaches everyone and flows back on the next sync; a fix sent to us would be
undone within a day.

**Coordinate quality is uneven and the interface admits it.** Of 476 venues, 223
matched the theatre itself, 224 matched only the town centre, and 29 matched
nothing. Reverse-geocoding a sample of the exact fixes put 10 of 12 on the
cinema, with the two misses landing on a neighbouring unit in the same complex.
The Maps link uses stored coordinates only for exact matches; for anything less
it hands Google the venue's name, whose place data resolves it better than a
city centroid would.

---

## 4. Things deliberately not done

- **No showtimes data.** Showtimes are licensed, change daily, and would need
  either a backend or an API key in view-source. The Showtimes link is a search.
- **No scraping of imax.com.** It blocks automated access including to its own
  `robots.txt`, and working around that — via a reader proxy, or by scraping a
  search engine for `imax.com/theatre/` URLs — was tried and rejected.
- **No stored search results.** Google's API terms forbid building databases
  from them and Brave's forbid caching without a specific paid plan. Every route
  to a stored imax.com deep link is closed; the search link is built at click
  time from data we own.
- **No third-party analytics, fonts, or scripts.** Icons are an inline SVG
  sprite; type is a system serif stack.

---

## 5. File map

```
sync.py            wiki -> theatres.sqlite3, with validation
geocode.py         OpenStreetMap -> coordinates and websites
export.py          theatres.sqlite3 -> web/data/venues.json -> dist/
serve.py           local server: the site, a read-only API, and the admin page

schema.sql         one venues table, an FTS index, revisions, venue_changes
theatres.sqlite3   the data and its entire history — committed on purpose

web/index.html     page shell and the inline icon sprite
web/app.js         rendering, tabs, filters, links
web/query.js       search, filter, sort, paginate — pure functions, tested
web/geo.js         country detection from timezone, no network
web/admin.html     local-only tooling, never published

test_sync.py       wikitext parsing
test_db.py         apply_revision and validate
test_serve.py      the read-only query layer behind the admin page
test_export.py     the publishing step and the no-admin guarantee
test_guards.py     loopback, Host and Origin checks on the local server
test_bundle.py     assertions on the JSON about to be served
test_query.mjs     the browser-side query logic (node --test)
smoke.py           loads the built site in a real browser
```
