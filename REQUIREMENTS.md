# FindMaxScreen — requirements & status

Source of truth for what this project must do and why it is built the way it is.
Written down so a machine restart, or a fresh session, can't lose the reasoning.

**Picking this up cold? Read `HANDOFF.md` first** — it has the current state and
the exact remaining steps to publish.

## Brief (as stated by the user)

Data source: <https://imax.fandom.com/wiki/List_of_IMAX_venues>

1. **Searchable dashboard** — a webpage listing IMAX venues, with the primary
   job being *find the 70 mm (15/70 film) theatres*.
2. **A Google Maps link** per venue.
3. **Local SQLite storage**, so the data survives restarts and is queryable
   offline.
4. **Manual update** — an explicit, user-triggered refresh from the wiki.
   (Superseded by 12: now also automatic, daily.)
5. **Tests** for the wiki parsing, and **validation after every update**.
6. **Personality in the UI**, with badges for 70 mm and the other formats.
7. **Newsprint theme** (as in Typora), light *and* dark, with monochrome icons.
8. **A legend** explaining which IMAX formats are actually better.
9. **Three sections** — everything; a "your country" view with the country
   guessed for the visitor; and a plain answer to whether 70 mm exists there.
10. **Refresh is admin-only** — it must not appear on the public site.
11. **Publish online** — a public site on a custom domain, fast worldwide.
12. **Deploy daily and unattended**, via GitHub Actions.
13. **Pagination** (25 per page) and a visible way to **reset filters**.
14. Renamed to **FindMaxScreen**, domain `findmaxscreen.com`, with a
    "why this exists" note in the header.
15. **A way to report problems** — one email link, not per-venue, and not a form
    service.
16. **Coordinates** from OpenStreetMap; directions and maps via Google.
17. **Generic filenames** (`theatres.sqlite3`) to avoid trademark friction —
    but **theatre names and page copy left exactly as they are**.

Layout requests, which went through several rounds and are recorded because the
final arrangement looks arbitrary otherwise: links **vertical on the trailing
edge**; the redundant *Directions* and per-venue *Report* removed; header and
footer spanning the **full width** rather than a reading measure; **louder
country headings**; a **location pin** on each venue's place line.

## Status

| # | Requirement | Status |
|---|---|---|
| 1 | Searchable dashboard, 70 mm first-class | **done** |
| 2 | Google Maps link per venue | **done** — built client-side in `venueLinks()` |
| 3 | Local SQLite persistence | **done** — `theatres.sqlite3`, `schema.sql` |
| 4 | Manual refresh | **done** — `./sync.py`, admin page |
| 5 | Parser tests + post-update validation | **done** — 158 Python tests, `validate()` |
| 6 | Badges and personality | **done** |
| 7 | Newsprint theme, light + dark, monochrome icons | **done** — inline SVG sprite |
| 8 | Format legend | **done** — the "IMAX types" tab |
| 9 | Three sections | **done** — tab bar, `web/geo.js` |
| 10 | Refresh is admin-only | **done** — excluded from `dist/` by allow-list |
| 11 | Publishable online, custom domain | **done** — `./export.py` → `dist/`, 49 KB gzipped |
| 12 | Daily unattended deploy | **built, not yet live** — `.github/workflows/daily.yml` |
| 13 | Pagination + reset filters | **done** — 25/page, filter chips |
| 14 | Renamed, domain, header note | **done** |
| 15 | Report a problem | **done** — one email link in the footer |
| 16 | Coordinates from OSM | **done** — 447 of 476 located |
| 17 | Generic filenames | **done** — venue names and copy untouched |

Verified 2026-07-31 against the live wiki: 476 venues, 58 with 15/70 mm film,
28 dome, 56 countries; 447 geocoded (223 exact, 224 city-level), 160 with a
cinema website. **158 Python + 33 JavaScript tests**, 22 bundle assertions and
16 headless smoke checks all pass; `--validate-only` reports zero findings.

**Not yet done:** the folder rename, `git init`, and the first push. See
`HANDOFF.md`.

## Why "15/70" shows 4 venues and the 70 mm toggle shows 58

This looks like a bug and is not one, so it is written down here rather than
re-investigated.

`film_family` is a **projector-model** classifier, not a film-format flag.
`FILM_FAMILIES` in `sync.py` tries GT3D → GT Dome → SR Dome → SR → GT → Dome in
order; when none match but the cell mentions 15/70, it falls back to the literal
family `"15/70"`. So that bucket means *"a 15/70 projector whose model the wiki
does not state"* — four rows written as a bare `IMAX 15/70 mm`. The six buckets
partition the 58 exactly:

    GT3D 25 + SR 12 + GT Dome 11 + SR Dome 5 + 15/70 4 + Dome 1 = 58

No row has a `film_family` without `has_70mm`, so the partition is total. The UI
now leads the Film projector select with an explicit **"Any 15/70 mm film (58)"**
entry that hands the filter to the 70 mm toggle, and renames the fallback bucket
to **"Model unspecified"**, so the relationship is visible rather than inferred.

## Bugs found and fixed on 2026-07-30

Each was a filter whose count disagreed with the data, and each now has a
regression test plus a validator check:

1. **`is_1_43` excluded every dome.** The flag was set with
   `screen_ar.startswith("1.43")`, but domes are written `Dome 1.43:1`. The
   filter reported 86 where 114 venues have a 1.43:1 screen. The flag now means
   "the screen is 1.43:1"; use the Dome toggle to separate flat from dome.
2. **An upstream typo hid three venues.** Three Vietnamese venues are written
   `1:90:1`. `_normalize_ar()` repairs the mistyped separator, taking the 1.90
   list from 338 to 341. `Dome 1.43:1` is deliberately *not* rewritten — the
   prefix is real information, not a typo.
3. **Zero-height screens stored as `0.0`.** Two venues are written `21.0 m × 0 m`
   — the height is unknown, not zero. Both now store NULL.
4. **Decimal commas inflated three areas 10x** (found earlier the same day; see
   the design decisions below).

## Validation

`validate(conn)` runs automatically at the end of every sync, and standalone via
`./sync.py --validate-only`. Findings are `ERROR` or `WARN`:

- **ERROR** — the database itself is wrong: FTS index drift, duplicate
  `venue_key`, a missing required field, an out-of-range `commercial_films`, an
  unknown region. Errors make the sync exit non-zero, and the "Refresh from
  wiki" button surfaces them.
- **WARN** — the upstream wiki is inconsistent: a derived flag disagreeing with
  the text it came from, a 15/70 venue whose model would not classify, screen
  measurements outside the range any real IMAX occupies. Warnings do not fail
  the sync; `--strict` makes them fail too.

The flag checks re-derive each boolean straight from the source text, so a filter
that silently stops matching fails here rather than in the UI — which is exactly
how bugs 1 and 3 above were caught in the stored data.

Note on the FTS check: counting `venues_fts` cannot detect drift, because it is
an external-content table and `count(*)` reads straight through to `venues` and
always agrees. The check counts the `venues_fts_docsize` shadow table, which is
the real index. (The first version of this check could never fire; the test for
it is what exposed that.)

## Tests

    python3 -m unittest discover -p 'test_*.py'      # 144 tests, no network

- `test_sync.py` — wikitext parsing: both column layouts, nested rowspans,
  diacritics, projector-name spellings, dimension and aspect-ratio normalization.
- `test_db.py` — `apply_revision` and `validate`: insert, no-op re-apply,
  field-level change logging, soft-delete, the re-appearance path, revision
  upsert, and each validator check firing on an injected defect.
- `test_serve.py` — the read-only query layer still used by the admin page:
  FTS tokenization, every filter, the three sorts, limit clamping, and facet
  counts agreeing with the result counts they label.
- `test_export.py` — the publishing step: payload fidelity, facet counts
  agreeing with the venues beside them, no provenance or raw wikitext leaking
  out, the timezone map, and the guarantee that admin never reaches `dist/`.
- `test_query.mjs` (`node --test`) — the search and filter logic that moved into
  the browser. `web/package.json` exists only so Node parses these `.js` files
  as ES modules, the way the browser does; it is not an npm project and is not
  published.
- `test_bundle.py` — assertions on the JSON about to be published rather than on
  the code that made it: stats agreeing with the venue list, facets reconciling,
  film70 non-zero, venue count within 15% of the committed database, geocode
  coverage, and the `geo_matched` coordinate audit.
- `smoke.py` — loads the built site in headless Chrome. Not a unit test: it is
  the only gate that runs the page rather than reasoning about it.

## Publishing

The public site is **static**: plain files, no server, no database, no API. All
searching and filtering happens in the browser over one JSON document. That is
both the simplest thing that works for 476 rows and the reason there is nothing
to attack.

```bash
./sync.py        # pull the wiki into theatres.sqlite3 (or use the admin page)
./geocode.py     # coordinates for anything new
./export.py      # validate, write web/data/venues.json, build dist/
```

`dist/` is six files, **49 KB gzipped in total**. It contains no server, no
database and no admin page.

### Hosting: GitHub Pages, published daily by Actions

Automated by `.github/workflows/daily.yml` — cron plus `workflow_dispatch`. Most
days the wiki revision has not moved and the run is one HTTP request.

**Cloudflare Pages was considered and rejected.** Recorded so it is not
re-argued: Cloudflare has ~300 edge cities against Fastly's smaller footprint,
unlimited bandwidth, private-repo support and `_headers` control. But at 49 KB a
visit, Pages' 100 GB/month soft cap is ~2 million visits; the repo is 516 KB
against a 1 GB limit; the site is not commercial. The real difference is *tens
of milliseconds* of TTFB, concentrated in India, South-East Asia, South America
and Africa. Against that, GitHub Pages needs **zero secrets and one account**,
which matters more for a job nobody watches. That last clause — if cache-header
control is ever wanted, Cloudflare's free CDN in front of Pages restores it
without moving the hosting — has since been exercised. See below.

The domain is **findmaxscreen.com**, held in `web/CNAME` and shipped inside the
artifact — Pages reads it from there and drops the custom domain on any deploy
that omits it, so it cannot live in repo settings alone.

One consequence accepted knowingly: the repo must be **public** on the free plan,
so `theatres.sqlite3` and `snapshots/` are public too — fine for CC BY-SA data
already being republished.

### Cloudflare fronts Pages

The prediction above was that a CDN in front would be worth *tens of
milliseconds*, concentrated in poorly-served regions. **Measurement said
otherwise, and it is the reason this was done.** From Chennai, 2026-07-31:

| Object state at Fastly | Server-side TTFB |
|---|---|
| edge HIT | **25–32 ms** |
| edge revalidating (`age: 0`) | **240–300 ms** |

Geography was never the problem — Pages' Fastly layer already serves this site
from a Chennai POP. The problem was the fixed **`max-age=600`** Pages hard-codes
and offers no way to change: every ten minutes each POP drops the object and the
next visitor pays a ~250 ms origin round-trip. On a low-traffic site that is not
an edge case, since a lone visitor in a ten-minute window *always* repopulates
the cache. And the content behind that TTL changes every few days at most —
`sync.py` no-ops on an unmoved revision. A ten-minute TTL on fortnightly data was
the whole inefficiency.

So the edge now holds objects for **one month** with a **ten-minute browser TTL**,
and `daily.yml` purges on every deploy. HTTP/3, which Pages does not offer, came
along with it.

**Brotli did not, in any meaningful amount** — worth recording because it was
predicted to matter and does not. Cloudflare compresses automatically now, but at
a fast, low-quality level, so measured against the edge on 2026-07-31:
`venues.json` was 27,656 B gzipped against 26,670 B Brotli — **3.6%**, not the
~13% assumed. Worse, when a real browser offers `gzip, deflate, br, zstd` the edge
picks *zstd* for HTML at 7,449 B, larger than either gzip (7,059 B) or Brotli
(6,974 B). Compression is a wash. The TTL is the whole win; do not re-litigate this
on compression grounds.

**SSL mode is `Full`, not `Full (strict)`, and that is deliberate.** Proxying stops
GitHub renewing its Let's Encrypt certificate (mechanism below), so the Pages
certificate will lapse after **2026-10-29** and its settings page will show a
certificate error. **This is expected and must not be "fixed."** Visitors never
see that certificate; they get Cloudflare's auto-renewing Universal SSL. It
governs only the invisible Cloudflare→GitHub hop, where under `Full` an expired
certificate is a non-event. Under `Full (strict)` the same expiry is a hard
`526` — the site *down* — avoidable only by grey-clouding DNS every ~60 days, a
manual chore whose failure mode is a silent countdown to an outage. What `Full`
gives up is authentication of one hop carrying a public, cookieless, auth-less
static site.

Automating that renewal dance in `daily.yml` was considered and rejected: it needs
the API token widened from *Cache Purge* to *DNS Edit*, putting a credential that
can rewrite the whole zone into CI — a worse risk than the one it fixes.

**`Full (strict)` is not reachable on this plan, and the reason is worth recording
so it is not re-investigated.** Note first that GitHub's certificate is not vestigial
under strict — Cloudflare presents SNI `findmaxscreen.com` on *every* origin fetch
and validates what comes back, so that certificate is load-bearing forever. Renewal
breaks because GitHub health-checks public DNS before provisioning: proxied, the
domain resolves to Cloudflare's IPs, GitHub reads that as a domain no longer pointing
at Pages, and never attempts renewal.

The clean fix is to stop asking GitHub for a `findmaxscreen.com` certificate at all
and target `findmaxscreen.github.io`, whose `*.github.io` wildcard GitHub renews
forever. That needs an Origin Rule overriding the `Host` header (which also rewrites
SNI) — and **Host header, SNI and DNS-record overrides are all Enterprise-only**. A
plain proxied CNAME to `findmaxscreen.github.io` does not substitute: the CNAME target
only selects the IP, while `Host` and SNI stay `findmaxscreen.com`. Pages accepts one
custom domain and allows no certificate upload, so there is no remaining angle.

The only free-tier routes to a validated origin hop both move the hosting — a Worker
fetching `findmaxscreen.github.io` (public-CA validated), or publishing to Workers
Static Assets so no origin hop exists. The second is the first without the extra hop;
neither is worth it to authenticate a public, cookieless, credential-free static site.

**The mode must be pinned to `Full` explicitly — never left on "Automatic
SSL/TLS."** Automatic mode rescans the origin roughly monthly and *upgrades* to
`Full (Strict)` whenever it finds a valid certificate, and by Cloudflare's own
documentation it **never downgrades**: "if your origin certificate expires, the
encryption mode will not change from Full (strict) to Full." On this zone that
composes into a timed outage — a scan sees GitHub's still-valid certificate and
upgrades to Strict, proxying then prevents renewal, and on expiry every visitor
gets a `526` with nothing left to correct it. Automatic mode would arrive at the
exact failure rejected above without anyone having chosen it. If the SSL/TLS
screen ever shows a "Next scan" date again, it has reverted; set it back to `Full`.

**The price is the "zero secrets" property above.** `daily.yml` now carries
`CLOUDFLARE_API_TOKEN` (scoped to Cache Purge on this zone alone) and
`CLOUDFLARE_ZONE_ID`. It also puts configuration outside version control, which
is why these must stay **off** in the dashboard:

- **Bot Fight Mode** — challenges non-browser clients. `robots.txt` deliberately
  allows crawling of `data/venues.json` and the JSON-LD advertises it as a public
  `Dataset`; it would also break this workflow's own `curl` gate. (Note the irony
  logged further down: a Cloudflare challenge is exactly why imax.com is unusable
  to us.)
- **Hotlink Protection** — `index.html` cites `og.png` and `apple-touch-icon.png`
  as absolute URLs; breaking them breaks link previews, and Apple caches the
  failure per URL.
- **Rocket Loader** — `app.js` is an ES module; Rocket Loader breaks module
  semantics.
- **Custom error pages** — the deploy gate asserts `admin.html` is a **404**. A
  catch-all returning 200 turns that gate green when it should be red.

Those four are default-off, and the security navigation has been moving — as of
July 2026 there is no `Security → Bots` section and no `Custom Pages` entry where
the docs put them. **Do not hunt for the toggles; test the behaviour**, which is
what actually matters and survives the next reorganisation:

```bash
# no bot challenge — every one of these must be 200
for ua in "Googlebot/2.1" "facebookexternalhit/1.1" "python-requests/2.31.0" ""; do
  curl -sS -o /dev/null -w "$ua -> %{http_code}\n" -A "$ua" \
    https://findmaxscreen.com/data/venues.json
done
# no hotlink protection — foreign referer must still get the image
curl -sS -o /dev/null -w 'og.png -> %{http_code}\n' \
  -e 'https://www.facebook.com/' https://findmaxscreen.com/og.png
# no custom error page — 404 body must be GitHub's, not Cloudflare's
curl -sS https://findmaxscreen.com/admin.html | grep -qi cloudflare \
  && echo 'REGRESSION: Cloudflare error page' || echo 'ok'
```

#### The dashboard as of July 2026

Cloudflare's UI moved faster than its own documentation while this was being set
up. Recorded so the next person does not hunt for controls that no longer exist:

- **There is no Brotli toggle.** Compression is automatic from the client's
  `Accept-Encoding`. Nothing to enable; see the measurement above for what it is
  worth.
- **`Speed → Optimization` is gone**; those toggles live under `Speed → Settings`.
  HTTP/3 and 0-RTT are under `Network`.
- **Smart Tiered Cache is no longer a standalone toggle** — it is inside
  **Smart Shield**, whose free base package is exactly Smart Tiered Cache plus
  Connection Reuse. Both are origin-side networking and neither rewrites HTML or
  scripts, so the Rocket Loader concern does not apply to it. **Take only the free
  base**: the same onboarding offers Argo Smart Routing, Health Checks and
  volumetric DDoS, which are billed.
- **`Cache Rules` and `Cache Response Rules` are now separate rule types.** Ours is
  a **Cache Rule** — that is the only type with cache eligibility and the Edge/
  Browser TTL overrides, and it runs in the request phase. Cache Response Rules run
  after the origin replies and exist to repair badly-behaved origin headers, which
  Pages does not send. Note for debugging: **where the two conflict, Cache Response
  Rules win**, so check for one of those before assuming the Cache Rule is at fault.

**`theatres.sqlite3` is committed on purpose.** It carries the entire audit trail —
soft-deletes, `venue_changes`, first-seen dates — plus all 476 geocodes. A job
starting from an empty database would lose that history *and* re-geocode every
venue daily, which would get the project blocked by Nominatim within a week.

### The deploy gate

Nothing publishes unless all of it passes; a failure leaves the previous
deployment live, so a bad run is a no-op rather than an outage.

1. **Unit tests** — 127 Python, 33 JavaScript. Run first, before any network.
2. **Shrink guard** (`sync.py`) — refuses a revision parsing to under 90% of the
   previous venue count.
3. **Validation** (`sync.py`) — ERROR findings exit non-zero.
4. **Bundle integrity** (`export.py`) — refuses to export an invalid database;
   `build_dist()` re-scans its output for anything admin-shaped.
5. **`test_bundle.py`** — assertions on the JSON about to be served: stats
   agreeing with the venue list, facet counts reconciling, film70 non-zero,
   venue count within 15% of the committed database, geocode coverage above
   80%, and a coordinate audit using `geo_matched`.
6. **`smoke.py`** — loads the built site in headless Chrome.
7. **Post-deploy** — fetches the live URL and checks the venue count matches and
   `/admin.html` 404s.

**Why `smoke.py` exists.** Extracting `paginate()` changed `renderPager`'s
signature and left the old call site passing a now-undefined variable. It threw
*after* the venue cards had rendered, so the page looked healthy while the pager
silently never appeared — and all 160 unit tests passed throughout. Unit tests
on an extracted function say nothing about its call site. Verified by fault
injection: re-introducing that exact bug into a copy of `dist/` makes
`smoke.py` fail four checks and exit 1.

**Two things learned building it**, both worth keeping:

- Chrome hangs indefinitely under `--dump-dom` when given a fresh
  `--user-data-dir`, leaving hundreds of MB behind. Measured: minimal 2.2s,
  `--no-sandbox` 2.1s, `--enable-logging` 2.2s, temp profile never returned.
  `smoke.py` omits it and kills the browser on timeout.
- `maps_url` is **not** exported — it was 55 KB of a deterministic string the
  page rebuilds from `name`/`city`/`country`. Assertions must check the fields
  that build it, not the field itself.

**Why the admin page is safe.** `POST /api/sync` shells out to `sync.py` as a
subprocess. Today that is guarded by `serve.py` refusing any client that is not
`127.0.0.1` — but a loopback check is no protection at all once something is
published, so the endpoint must simply not exist in public. It doesn't:

- The only page that calls it is `web/admin.html`, and `export.py` builds
  `dist/` from an **allow-list** (`PUBLIC_FILES`). Admin is not hidden or
  disabled; it is never copied.
- After copying, `build_dist()` re-scans the output and raises if anything
  matching `admin`, `sync.py`, `serve.py` or `.sqlite3` is present.
- Two tests assert both halves: that admin is absent from `dist/`, and that it
  still exists locally so the tooling keeps working.
- The public page links to admin only when `location.hostname` is localhost.

**`export.py` refuses to publish a database that fails validation.** Pushing a
known-wrong dataset to the internet is worse than not pushing, so an ERROR-level
finding aborts the export unless `--allow-invalid` is passed.

**Licensing.** The source page is Fandom content under CC BY-SA. The footer
carries the attribution and links to both the
[licence deed](https://creativecommons.org/licenses/by-sa/3.0/) and
[Fandom's licensing terms](https://www.fandom.com/licensing), states that this
page is a derived work offered under the same licence, and disclaims any
affiliation with IMAX Corporation or Fandom. Keep all of that if you deploy it.
Share-alike reaches the derived dataset, so this cannot go behind anything
proprietary.

## Coordinates, maps and showtimes

The wiki carries no coordinates, so `geocode.py` fetches them once from
**OpenStreetMap/Nominatim** and stores them in `venues.lat` / `venues.lon`.

**Why not Google Geocoding**, which is more accurate and free at this volume:
their terms cap caching at 30 days and tie indefinite storage of lat/lng to
their own map surface. The entire point here is to bake coordinates into a
static file and serve them forever, which those clauses are written against. OSM
data is ODbL — permanent storage is fine with attribution, which the footer
carries. Nominatim's policy allows a one-off bulk run at ≤1 request/second with
a real User-Agent; `MIN_INTERVAL` enforces it.

**Links go to Google Maps anyway**, and that is not a contradiction: a Maps URL
is a plain hyperlink needing no key and no API, and it is where people already
keep their reviews and navigation. Only OSM coordinates are *stored*.

- **Maps** — the place card, by coordinate when we have an exact one.
- **Directions** — `maps/dir/?api=1&destination=<lat>,<lon>`, shown only when
  the venue is geocoded. For city-level hits the tooltip says so rather than
  implying door-to-door accuracy.
- **Showtimes** — a search link, deliberately not hosted data. Showtimes are
  licensed (MovieGlu, Gracenote), change daily, and would need either a backend
  or an API key exposed in view-source — all three of which break the static,
  nothing-to-attack model. imaxnearme.com reaches the same conclusion: their
  "Showtimes" button is `venue.imax_url + "/movies"`, a deep link, not data.

### Why we do not deep-link to imax.com

A link straight to a theatre's own page on imax.com would be one click instead
of two, and it was attempted. It was abandoned deliberately:

1. **Deriving the slug is unreliable.** `AMC Metreon 16 & IMAX` is filed by IMAX
   as *AMC Loews* Metreon 16, and `Dendy Canberra & IMAX` as
   `dendy-canberra-and-imax`. Derivation scored about 3 in 8 even on US/UK
   venues, and failures are silent — a bad slug returns HTTP 200 with the
   generic finder, never a 404.
2. **imax.com blocks automated access outright**, answering every request with
   HTTP 403 — *including `/robots.txt` and `/sitemap.xml`*, which sit behind a
   Cloudflare challenge. When a site puts a bot challenge in front of the file
   whose entire purpose is to state its automation policy, the only honest
   reading is that automation is unwelcome, and the policy itself cannot even
   be retrieved.
3. **The workarounds are circumvention.** Reading the pages through a rendering
   proxy, or scraping DuckDuckGo's HTML endpoint for `imax.com/theatre/` URLs
   (which is how imaxnearme.com does it), both exist to get around that block.
   DuckDuckGo blocked this project's IP after roughly fifteen queries, which is
   the predictable result of scraping a search engine against its terms.

That another project does it is not a licence. The search link needs no key, no
scraping and no circumvention, works for all 476 venues, and never goes stale —
at the cost of one extra click. If a deep link is ever wanted, the clean route
is a keyed search API with terms that permit it (Brave's free tier covers 476
lookups), not a workaround.

**Name matching matters more than the geocoder.** Querying Nominatim with the
wiki's names scored **zero** exact hits: OSM records cinemas under their trading
name, so "Dendy Canberra & IMAX" is mapped as "Dendy Cinema" and "IMAX, Melbourne
Museum" as "Melbourne Museum". `search_names()` strips the IMAX ornamentation
and tries that first, which turned 0/8 into 5/8. Each result records
`geo_precision`: `venue` (the theatre), `city` (only the town centre) or `none`.
A venue hit landing more than `MAX_KM_FROM_CITY` from its own city is rejected
in favour of the city centre — that is the usual Nominatim failure, a similarly
named place elsewhere in the country.

Coordinates are **not** in `TRACKED_FIELDS`, so a wiki sync can never overwrite
them, and two validator checks keep them honest: coordinates and precision must
travel together, and latitude/longitude must be in range.

## Locating the visitor

There is no server to read an IP, and that turned out to be the better design.
The browser knows its own IANA timezone, and a timezone identifies a country
about as well as an IP does — with no permission prompt, no network request and
no visitor address leaving the machine. Order of preference in `web/geo.js`:

1. a country the visitor picked before (`localStorage`)
2. timezone → ISO code, via a table `export.py` reads from the OS tz database
3. the region subtag of the browser's language (`en-IN` → `IN`)

Notes worth keeping:

- The table is generated from `/usr/share/zoneinfo/zone*.tab`, not hand-written,
  so it stays right as zones are added. `TZ_ALIASES` patches the pre-rename
  spellings browsers still report — Chrome says `Asia/Calcutta`, not
  `Asia/Kolkata`, and without the alias every Indian visitor fell through.
- `COUNTRY_CODES` maps ISO codes back to the spelling the wiki uses. A test
  asserts every country in the live database has one, and `export.py` warns if
  the wiki adds a country that doesn't — otherwise visitors there would silently
  stop being recognised.
- The guess is always shown *as* a guess, with a visible override.

## How it fits together

```
                          LOCAL                          |        PUBLIC
                                                         |
imax.fandom.com --MediaWiki API--> sync.py               |
                                      |                  |
          snapshots/*.wiki <----------+ (every fetch)     |
                                      |                  |
                                 theatres.sqlite3            |
                                    /      \             |
                    serve.py (127.0.0.1)   export.py ----+--> dist/
                            |                            |     index.html
                     web/admin.html                      |     app.js query.js geo.js
                     POST /api/sync                      |     style.css
                     (never published) -------X----------|     data/venues.json
                                                         |     (no server, no admin)
```

- `./sync.py` — fetch + parse + apply + validate. No-ops if the wiki revision
  hasn't moved; `--force` re-parses anyway, `--dry-run` reports without writing,
  `--validate-only` checks the existing database, `--strict` fails on warnings.
- `./serve.py` — <http://127.0.0.1:8787>. Read-only `GET /api/{venues,facets,meta,changes}`
  plus `POST /api/sync` (the manual refresh, shells out to `sync.py`).
- `./export.py` — validate, write `web/data/venues.json`, build `dist/`.
- `python3 -m unittest discover -p 'test_*.py'` — 120 Python tests, no network.
- `node --test test_query.mjs` — 27 tests for the browser-side query logic.

## Design decisions worth keeping

- **Revision id is the change key.** Fandom returns HTTP 402 to plain HTML
  fetches, so we read raw wikitext via the MediaWiki API — which hands back a
  revid for free.
- **Nothing is ever DELETEd.** Venues that vanish upstream get `removed_at` set;
  `venue_changes` records every field-level edit. A bad wiki edit can't destroy
  local history.
- **Shrink guard.** A revision parsing to <90% of the previous venue count is
  refused (vandalism / table restructure). Override with `--allow-shrink`.
- **Maps links are search URLs**, not place-id deep links: the wiki has no
  coordinates or place ids, so `maps.google.com/maps/search/?api=1&query=<name,
  city, country>` is the accurate option. It lands on the place card, where the
  ratings and reviews already are — which is why there is **one** link per venue
  and not a separate "Reviews" one.
- **Commas mean two different things** in the screen-dimensions cells: German
  rows write `203,8 m²` (decimal) while imperial figures write `2,194 sq ft`
  (thousands). Stripping all commas inflated three German areas by 10x and put
  them at the top of the "biggest screen" sort. `_normalize_numbers` resolves it
  by digit count, and a stated area that disagrees with width x height by >20%
  is now flagged in `data_notes` rather than silently trusted.
- **Loopback only.** `serve.py` refuses non-127.0.0.1 clients; all GET traffic
  uses a `mode=ro` SQLite connection.

## The sections

Four tabs, not separate pages. Each sets a **scope** and the filter bar refines
within it; each also hides the control it already decides, so the heading and
the filters can never claim different things.

- **All venues** — everything, as before.
- **Your country** — scoped to the detected country, with a picker. Opens with
  a count of what is there and an italic line saying how the guess was made.
- **IMAX types** — the format guide. It was a collapsed `<details>` buried under
  the filters, which is a poor home for the one thing explaining what every
  badge means; it now has a tab of its own, and the whole filter/result
  apparatus is hidden while it is open.
- **70 mm film** — scoped to `has_70mm`, and opens with a plain Yes/No to "is
  there 15/70 mm film IMAX in <your country>?". When the answer is no, it names
  the nearest countries in the same region that do have one — region is the only
  geography this dataset carries, since the wiki has no coordinates.

The 70 mm section ends with a call to action rather than leaving you to work
out which control narrows the list: "Want to see just those?" sets the country
filter, or the region filter when the answer was no, and turns into "Show all 58
worldwide" once something is narrowed.

**Paginated** at 50 per page — 476 rows in one scroll is a wall. The controls
are rendered **both** beside the result count and under the list: a pager only
at the foot sits 50 rows down, which is a pager nobody finds. `paginate()` lives
in `query.js` rather than the render code because the interesting part is the
clamp: narrowing a filter can shrink the results below the page you were on, and
a blank page reads as "no results". Six tests cover it, including that every page
together covers the list exactly once.

A caution learned the hard way: those six tests all passed while the pager was
invisible in the browser. Extracting `paginate()` changed `renderPager`'s
signature and left the old call site passing a now-undefined variable, so it
threw *after* the venue cards had rendered — the page looked healthy and the
pager silently never appeared. Unit tests on an extracted function say nothing
about its call site; re-render the page and look at it.

**Active filters are chips, not a lone "Clear" button.** A `FILTERING BY` row
appears whenever anything is narrowing the list, naming each filter with an ×
to drop it individually, plus `Reset filters`. Filters a section already implies
are omitted — you cannot switch off the thing that defines the section you are
in.

Two CSS traps worth remembering:

- The UA stylesheet gives `[hidden]` `display: none`, but any author rule that
  sets `display` outranks it. `.toggle` is `inline-flex`, so the controls each
  tab hides stayed visible until `[hidden] { display: none !important }`.
- `<footer class="wrap">` inherits `margin: 0 auto` from `.wrap`, and a `margin`
  shorthand on `footer` silently dropped the centring, leaving the whole footer
  flush left under a centred page. The footer is now full-bleed with an inner
  `.wrap`, mirroring `.masthead`, so its double rule spans the viewport like the
  one under the header.

## Reporting a problem

Three audiences, three destinations, because one route cannot serve all of them:

| Who | What they hit | Where it goes |
|---|---|---|
| Anyone using the site | **Report** on each venue card, and a footer line | Prefilled email to `issues@findmaxscreen.com` |
| Someone correcting venue facts | The wiki link, named first in the footer | The IMAX Wiki — flows back on the next sync |
| Developers | Issue templates under `.github/ISSUE_TEMPLATE/` | GitHub issues, once `REPO_URL` is set |

**Why a mailto and not a form.** A static site has nowhere to POST to, and a
form service would mean a third party plus an endpoint id in the page. Most
people who spot a wrong link have never used GitHub and will not open an account
to report one, so requiring one means hearing nothing. The address is a *role*
address on our own domain — filterable and rotatable, and it exposes nobody's
personal inbox. The cost is that a published address gets scraped; that is the
price of not requiring an account, and a test asserts the local part stays a
role name rather than drifting to a personal one.

**Why the wiki is named first.** Projector, screen, name and location are
overwritten from the wiki on every sync (they are in `TRACKED_FIELDS`), so a
correction sent here would be undone within a day. Coordinates, `website` and
`geo_precision` are deliberately *not* tracked and do survive. Anyone
contributing data needs to know which side of that line they are on.

The report is prefilled with what the page currently believes — projector,
screen, mapping precision, the venue website — so the reporter need not retype
it and the reader can tell at a glance whether the parser or the wiki is wrong.
`encodeURIComponent` rather than `URLSearchParams`, since the latter encodes a
space as `+`, which mail clients render literally in a subject line.

## Look and feel

- **Newsprint.** The palette and typography follow Typora's Newsprint theme:
  warm paper (`#f3f2ee`), near-black serif text, oxblood accents, and hairline
  rules instead of boxed cards. A cinema listing is a listings page, so it reads
  like one. Fonts are a system serif stack (Iowan Old Style → Palatino → PT
  Serif → Georgia); nothing is downloaded.
- **One palette, two themes.** Every colour is declared once with CSS
  `light-dark()`, and the theme button flips `color-scheme` on `:root` — so
  there is no second copy of the palette to keep in sync. The button cycles
  Auto → Light → Dark and remembers the choice in `localStorage`; Auto follows
  the OS. The dark palette is a warm "evening edition" charcoal, not a grey
  inversion.
- **Icons are inline SVG**, drawn in `index.html` as a `<symbol>` sprite and
  referenced with `<use>`. Monochrome, `currentColor`, so one drawing serves
  both themes and there is no icon font or CDN. Sprite entries must be
  `<symbol viewBox="0 0 24 24">` — a plain `<g>` carries no viewBox, so `<use>`
  renders it 1:1 into a 15px box and you see only the top-left corner. Keep
  glyphs to a few bold strokes; anything more detailed turns to mush at badge
  size.
- **The legend** (`<details>` above the results) ranks the formats by picture
  quality and doubles as a key to the badges — each entry uses the same badge
  markup the result rows do, so they only have to be learned once.

## Not doing (unless asked)

- Scheduled/automatic syncs — `launchd/` is empty on purpose; requirement 4 is
  explicitly manual.
- Any hosting beyond localhost.
