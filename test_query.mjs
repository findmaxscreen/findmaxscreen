/* Tests for web/query.js - the search and filter logic that moved into the
 * browser when the site became static.
 *
 * This is the code that decides what a filter count says, and every filter bug
 * this project has had was a count disagreeing with the data, so it is worth
 * testing directly rather than only through screenshots.
 *
 *     node --test test_query.mjs
 */

import test from "node:test";
import assert from "node:assert/strict";

import {
  fold, tokenize, index, search, countryReport, paginate, haversineKm,
  cityIndex,
} from "./web/query.js";

function venue(name, over = {}) {
  return {
    id: over.id ?? name.length, region: "Europe", country: "Testland",
    state: "", city: "Testville", name, maps_url: "https://example.invalid",
    screen_ar: "1.90:1", digital_projector: "IMAX CoLa",
    projector_family: "CoLa", max_digital_ar: "1.90:1", film_projector: "",
    film_family: "", has_70mm: 0, is_dome: 0, is_1_43: 0, is_temporary: 0,
    commercial_films: 1, screen_w_m: null, screen_h_m: null,
    screen_area_m2: null, data_notes: "", removed_at: null, ...over,
  };
}

const FIXTURES = index([
  venue("Flat 70", {
    country: "Canada", city: "Vancouver", screen_ar: "1.43:1", is_1_43: 1,
    has_70mm: 1, film_family: "GT3D", film_projector: "IMAX GT3D 15/70 mm",
    projector_family: "GT Laser", screen_area_m2: 533,
  }),
  venue("Dome 70", {
    country: "Belgium", city: "Bruxelles", screen_ar: "Dome 1.43:1",
    is_1_43: 1, is_dome: 1, has_70mm: 1, film_family: "SR Dome",
    projector_family: "Dome Laser", commercial_films: 0, screen_area_m2: 300,
  }),
  venue("Bare 70", {
    country: "United States", state: "CA", city: "Irvine",
    screen_ar: "1.43:1", is_1_43: 1, has_70mm: 1, film_family: "15/70",
  }),
  venue("Digital only", {
    country: "Japan", city: "Tokyo", screen_area_m2: 200,
  }),
  venue("Sao Paulo Cinema", {
    country: "Brazil", region: "Americas", city: "São Paulo",
  }),
  venue("Closed Cinema", {
    country: "Atlantis", city: "Nowhere", removed_at: "2026-02-01T00:00:00+00:00",
  }),
]);

const names = (criteria) => search(FIXTURES, criteria).venues.map((v) => v.name);

test("fold strips accents and case", () => {
  assert.equal(fold("São Paulo"), "sao paulo");
  assert.equal(fold("Liège"), "liege");
  assert.equal(fold(""), "");
  assert.equal(fold(undefined), "");
});

test("tokenize drops punctuation and keeps words", () => {
  assert.deepEqual(tokenize("van cou"), ["van", "cou"]);
  assert.deepEqual(tokenize("  !!!  "), []);
  assert.deepEqual(tokenize('a" OR b'), ["a", "or", "b"]);
});

test("prefix search finds a city", () => {
  assert.deepEqual(names({ q: "vancou" }), ["Flat 70"]);
});

test("search is diacritic-insensitive both ways", () => {
  assert.deepEqual(names({ q: "sao paulo" }), ["Sao Paulo Cinema"]);
  assert.deepEqual(names({ q: "São" }), ["Sao Paulo Cinema"]);
});

test("all tokens must match", () => {
  assert.deepEqual(names({ q: "dome 70" }), ["Dome 70"]);
  assert.deepEqual(names({ q: "dome zzz" }), []);
});

test("a word inside the name is findable, not just the first", () => {
  assert.deepEqual(names({ q: "cinema" }), ["Sao Paulo Cinema"]);
});

test("nonsense finds nothing", () => {
  assert.equal(search(FIXTURES, { q: "zzznotathing" }).total, 0);
});

test("film70 returns exactly the 15/70 venues", () => {
  assert.deepEqual(names({ film70: true }).sort(),
    ["Bare 70", "Dome 70", "Flat 70"]);
});

test("dome and commercial filters", () => {
  assert.deepEqual(names({ dome: true }), ["Dome 70"]);
  assert.ok(!names({ commercial: true }).includes("Dome 70"));
});

test("country and family filters", () => {
  assert.deepEqual(names({ country: "Canada" }), ["Flat 70"]);
  assert.deepEqual(names({ film: "GT3D" }), ["Flat 70"]);
  assert.deepEqual(names({ projector: "Dome Laser" }), ["Dome 70"]);
});

test("filters combine", () => {
  assert.deepEqual(names({ film70: true, dome: true }), ["Dome 70"]);
});

test("1.43 includes domes", () => {
  // Regression guard: "Dome 1.43:1" is still a 1.43 screen. Matching the raw
  // string by prefix dropped all 28 domes from this filter in the real data.
  assert.deepEqual(names({ ar: "1.43" }).sort(),
    ["Bare 70", "Dome 70", "Flat 70"]);
});

test("1.43 plus the dome toggle narrows to domes", () => {
  assert.deepEqual(names({ ar: "1.43", dome: true }), ["Dome 70"]);
});

test("1.90 matches on the ratio prefix", () => {
  assert.ok(names({ ar: "1.90" }).includes("Digital only"));
  assert.ok(!names({ ar: "1.90" }).includes("Flat 70"));
});

test("de-listed venues are hidden unless asked for", () => {
  assert.ok(!names({}).includes("Closed Cinema"));
  assert.ok(names({ includeRemoved: true }).includes("Closed Cinema"));
});

test("total counts matches, shown counts what was returned", () => {
  const result = search(FIXTURES, { limit: 2 });
  assert.equal(result.total, 5);
  assert.equal(result.shown, 2);
});

test("size sort is biggest first with unknowns last", () => {
  const rows = search(FIXTURES, { sort: "size" }).venues;
  const areas = rows.map((v) => v.screen_area_m2);
  const known = areas.filter((a) => a !== null);
  assert.deepEqual(known, [...known].sort((a, b) => b - a));
  assert.deepEqual(areas.slice(known.length), areas.slice(known.length).map(() => null));
});

test("name sort is case-insensitive alphabetical", () => {
  const sorted = names({ sort: "name" });
  assert.deepEqual(sorted, [...sorted].sort((a, b) =>
    a.toLowerCase() < b.toLowerCase() ? -1 : 1));
});

test("location sort groups by country then city", () => {
  const rows = search(FIXTURES, { sort: "location" }).venues;
  const countries = rows.map((v) => v.country);
  assert.deepEqual(countries, [...countries].sort());
});

test("an unknown sort does not throw", () => {
  assert.deepEqual(names({ sort: "nonsense" }), names({ sort: "location" }));
});

test("search does not mutate its input", () => {
  const before = FIXTURES.map((v) => v.name);
  search(FIXTURES, { sort: "size", film70: true });
  assert.deepEqual(FIXTURES.map((v) => v.name), before);
});

test("paginate splits a list into pages", () => {
  assert.deepEqual(paginate(476, 1, 50),
    { page: 1, pages: 10, from: 0, to: 50, hasPrev: false, hasNext: true, needed: true });
  assert.deepEqual(paginate(476, 10, 50),
    { page: 10, pages: 10, from: 450, to: 476, hasPrev: true, hasNext: false, needed: true });
});

test("paginate clamps a page beyond the end", () => {
  // Narrowing a filter can shrink the results below the page you were on;
  // landing on a blank page reads as "no results".
  assert.equal(paginate(12, 99, 50).page, 1);
  assert.equal(paginate(120, 99, 50).page, 3);
});

test("paginate clamps nonsense page numbers", () => {
  for (const bad of [0, -3, NaN, undefined, 1.7]) {
    assert.equal(paginate(476, bad, 50).page, 1, `page ${bad}`);
  }
});

test("paginate reports no pager needed for a short list", () => {
  assert.equal(paginate(50, 1, 50).needed, false);
  assert.equal(paginate(51, 1, 50).needed, true);
});

test("paginate handles an empty result set", () => {
  const slice = paginate(0, 1, 50);
  assert.equal(slice.pages, 1);
  assert.equal(slice.from, 0);
  assert.equal(slice.to, 0);
  assert.equal(slice.needed, false);
});

test("every page together covers the whole list exactly once", () => {
  const total = 476, size = 50;
  const seen = [];
  for (let p = 1; p <= paginate(total, 1, size).pages; p++) {
    const s = paginate(total, p, size);
    for (let i = s.from; i < s.to; i++) seen.push(i);
  }
  assert.deepEqual(seen, [...Array(total).keys()]);
});

test("country report answers the 70 mm question", () => {
  const canada = countryReport(FIXTURES, "Canada");
  assert.equal(canada.venues, 1);
  assert.equal(canada.film70, 1);
  assert.deepEqual(canada.nearby, []);
});

test("country report names regional alternatives when the answer is no", () => {
  const japan = countryReport(FIXTURES, "Japan");
  assert.equal(japan.film70, 0);
  // Japan is filed under Europe in these fixtures, so the regional fallback
  // should offer the European film houses rather than every country on earth.
  assert.deepEqual(japan.nearby.map((c) => c.country).sort(),
    ["Belgium", "Canada", "United States"]);
});

test("country report ignores de-listed venues", () => {
  assert.equal(countryReport(FIXTURES, "Atlantis").venues, 0);
});

test("a country with no venues at all reports zeroes", () => {
  const nowhere = countryReport(FIXTURES, "Narnia");
  assert.equal(nowhere.venues, 0);
  assert.equal(nowhere.film70, 0);
  assert.equal(nowhere.region, null);
});

/* ------------------------------------------------------------- distance
 *
 * The "Nearest first" sort. The interesting cases are not the arithmetic but
 * what happens when the inputs are missing: 29 real venues have no coordinates
 * at all, and the sort can be selected before - or without ever - a position
 * being granted.
 */

const LONDON = { lat: 51.5074, lon: -0.1278 };

const GEO = index([
  venue("Paris", { country: "France", city: "Paris", lat: 48.8566, lon: 2.3522 }),
  venue("Edinburgh", { country: "UK", city: "Edinburgh", lat: 55.9533, lon: -3.1883 }),
  venue("Sydney", { country: "Australia", city: "Sydney", lat: -33.8688, lon: 151.2093 }),
  venue("Nowhere", { country: "Thailand", city: "Bangkok", lat: null, lon: null }),
]);

const near = (criteria) => search(GEO, criteria).venues.map((v) => v.name);

test("haversine matches a known distance", () => {
  // London to Paris is ~344 km; agreeing to the kilometre is plenty.
  assert.ok(Math.abs(haversineKm(51.5074, -0.1278, 48.8566, 2.3522) - 344) < 1);
  assert.equal(haversineKm(10, 20, 10, 20), 0);
});

test("distance sort orders by proximity to the origin", () => {
  assert.deepEqual(near({ sort: "distance", origin: LONDON }),
    ["Paris", "Edinburgh", "Sydney", "Nowhere"]);
});

test("distance sort follows the origin rather than the alphabet", () => {
  // From Sydney the order reverses; alphabetical order would not have moved.
  assert.deepEqual(near({ sort: "distance", origin: { lat: -33.8688, lon: 151.2093 } }),
    ["Sydney", "Edinburgh", "Paris", "Nowhere"]);
});

test("venues with no coordinates sort last, not as distance zero", () => {
  const rows = search(GEO, { sort: "distance", origin: LONDON }).venues;
  assert.equal(rows.at(-1).name, "Nowhere");
  assert.equal(rows.at(-1)._km, null);
});

test("distance falls back to location order without an origin", () => {
  // A shared ?sort=distance link, or a refused prompt: the list still has to
  // come out in a defensible order.
  assert.deepEqual(near({ sort: "distance" }),
    near({ sort: "location" }));
});

test("distance measures only the rows that survived the filters", () => {
  const result = search(GEO, { sort: "distance", origin: LONDON, country: "France" });
  assert.deepEqual(result.venues.map((v) => v.name), ["Paris"]);
  assert.ok(Math.abs(result.venues[0]._km - 344) < 1);
});

test("distance sort leaves the total alone", () => {
  const result = search(GEO, { sort: "distance", origin: LONDON });
  assert.equal(result.total, 4);
  assert.equal(result.shown, 4);
});

test("cityIndex maps each city once, alphabetically, skipping the unmapped", () => {
  const cities = cityIndex(GEO.concat(index([
    venue("Second Paris venue", { country: "France", city: "Paris", lat: 48.9, lon: 2.4 }),
  ])));
  // "Nowhere" has no coordinates, so Bangkok cannot be measured from.
  assert.deepEqual([...cities.keys()],
    ["Edinburgh, UK", "Paris, France", "Sydney, Australia"]);
  // First venue with coordinates wins; the duplicate does not overwrite it.
  assert.equal(cities.get("Paris, France").lat, 48.8566);
});

test("a named city ranks exactly like a fix from that city", () => {
  const cities = cityIndex(GEO);
  const origin = cities.get("Paris, France");
  assert.deepEqual(near({ sort: "distance", origin }),
    ["Paris", "Edinburgh", "Sydney", "Nowhere"]);
});
