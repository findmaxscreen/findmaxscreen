/* Searching, filtering and sorting - the work serve.py's SQL used to do.
 *
 * The published site has no backend, so this runs in the browser over the whole
 * dataset.  476 rows is small enough that a linear pass per keystroke is
 * imperceptible, and it keeps the logic in one place instead of split across a
 * query string and a SQL builder.
 *
 * Everything here is a pure function of its arguments so it can be tested
 * directly under `node --test`; the filter bugs this project has already had
 * were all of the "count disagrees with the data" kind, which is exactly what
 * that catches.
 */

/** Fold case and strip accents, so "sao paulo" matches "São Paulo". */
export function fold(text) {
  return (text || "")
    .normalize("NFD")
    .replace(/\p{Diacritic}/gu, "")
    .toLowerCase();
}

const WORD = /[^\W_]+/gu;

/** Split free text into search tokens, dropping punctuation. */
export function tokenize(text) {
  return fold(text).match(WORD) || [];
}

/**
 * Attach the folded text each venue is searched against.  Done once on load
 * rather than per keystroke; the fields match the FTS index serve.py builds.
 */
export function index(venues) {
  return venues.map((v) => ({
    ...v,
    _name: fold(v.name),
    _place: [v.city, v.state, v.country].filter(Boolean).map(fold).join(" "),
  }));
}

/** True when every token prefixes some word in the venue's name or place. */
function matches(venue, tokens) {
  return tokens.every((token) =>
    venue._name.startsWith(token) ||
    venue._name.includes(` ${token}`) ||
    venue._place.startsWith(token) ||
    venue._place.includes(` ${token}`));
}

const COLLATOR = new Intl.Collator("en", { sensitivity: "base", numeric: true });

/**
 * Great-circle distance between two points, in kilometres.
 *
 * The same formula geocode.py uses to sanity-check a match, so the browser and
 * the geocoder agree about what "near" means.  Accuracy is far better than the
 * data warrants - half the venues are only located to their city - which is why
 * the UI rounds hard rather than printing a decimal.
 */
export function haversineKm(aLat, aLon, bLat, bLon) {
  const rad = Math.PI / 180;
  const dLat = (bLat - aLat) * rad;
  const dLon = (bLon - aLon) * rad;
  const h = Math.sin(dLat / 2) ** 2
    + Math.cos(aLat * rad) * Math.cos(bLat * rad) * Math.sin(dLon / 2) ** 2;
  return 2 * 6371 * Math.asin(Math.sqrt(h));
}

const SORTS = {
  location: (a, b) =>
    COLLATOR.compare(a.country, b.country) ||
    COLLATOR.compare(a.state, b.state) ||
    COLLATOR.compare(a.city, b.city) ||
    COLLATOR.compare(a.name, b.name),

  name: (a, b) => COLLATOR.compare(a.name, b.name),

  // Reads the `_km` search() attaches when an origin is known. 29 venues have
  // no coordinates at all - Nominatim missed them - and they sort last rather
  // than counting as distance zero, the same way size treats an unknown area.
  distance: (a, b) => {
    const x = a._km, y = b._km;
    if (x == null && y == null) return SORTS.location(a, b);
    if (x == null) return 1;
    if (y == null) return -1;
    return x - y || COLLATOR.compare(a.name, b.name);
  },

  // Biggest screen first, with unknown areas last rather than treated as zero.
  size: (a, b) => {
    const x = a.screen_area_m2, y = b.screen_area_m2;
    if (x === null && y === null) return COLLATOR.compare(a.name, b.name);
    if (x === null) return 1;
    if (y === null) return -1;
    return y - x || COLLATOR.compare(a.name, b.name);
  },
};

export const SORT_NAMES = Object.keys(SORTS);

/**
 * Filter and sort an indexed venue list.
 *
 * The caller's section decides the scope - a country, or 15/70 mm film - and
 * the filter controls refine within it.
 *
 * `origin` is {lat, lon} when the visitor has handed over a position, and is
 * what the "distance" sort needs; without one that sort degrades to "location".
 *
 * "location" is not on the sort menu but is still the default and still the
 * fallback for anything unrecognised: it is the only order that needs nothing
 * from the visitor, and it is what draws the country headings.
 */
export function search(venues, criteria = {}) {
  const {
    q = "", country = "", region = "", projector = "", film = "",
    film70 = false, dome = false, commercial = false, ar = "",
    includeRemoved = false, sort = "location", limit = 0, origin = null,
  } = criteria;

  const tokens = tokenize(q);
  let rows = venues;

  if (!includeRemoved) rows = rows.filter((v) => v.removed_at === null);
  if (country) rows = rows.filter((v) => v.country === country);
  if (region) rows = rows.filter((v) => v.region === region);
  if (projector) rows = rows.filter((v) => v.projector_family === projector);
  if (film) rows = rows.filter((v) => v.film_family === film);
  if (film70) rows = rows.filter((v) => v.has_70mm === 1);
  if (dome) rows = rows.filter((v) => v.is_dome === 1);
  if (commercial) rows = rows.filter((v) => v.commercial_films === 1);

  // 1.43 goes through the flag, not the raw string: dome venues are written
  // "Dome 1.43:1" and matching on a prefix silently dropped all of them.
  if (ar === "1.43") rows = rows.filter((v) => v.is_1_43 === 1);
  else if (ar) rows = rows.filter((v) => v.screen_ar.startsWith(ar));

  if (tokens.length) rows = rows.filter((v) => matches(v, tokens));

  const total = rows.length;

  // Measured once per row, before sorting rather than inside the comparator:
  // sort() calls that ~n log n times for a value that depends only on the row.
  // Only the rows that survived the filters are measured.
  if (sort === "distance" && origin) {
    rows = rows.map((v) => ({
      ...v,
      _km: v.lat == null || v.lon == null
        ? null
        : haversineKm(origin.lat, origin.lon, v.lat, v.lon),
    }));
  }

  // The control outlives the fix: a shared ?sort=distance link arrives before
  // any permission exists, and a denial leaves the choice standing. Falling
  // back keeps the list in a defensible order instead of an arbitrary one.
  const order = sort === "distance" && !origin
    ? SORTS.location
    : SORTS[sort] || SORTS.location;

  rows = [...rows].sort(order);
  if (limit > 0 && rows.length > limit) rows = rows.slice(0, limit);
  return { total, shown: rows.length, venues: rows };
}

/**
 * Work out which slice of a result set to show.
 *
 * Kept here rather than in the render code because the interesting case is not
 * the arithmetic but the clamp: narrowing a filter can shrink the results below
 * the page you were on, and landing on a blank page reads as "no results".
 */
export function paginate(total, page, size) {
  const pages = Math.max(1, Math.ceil(total / size));
  const current = Math.min(Math.max(1, Math.floor(page) || 1), pages);
  const from = (current - 1) * size;
  const to = Math.min(from + size, total);
  return {
    page: current, pages, from, to,
    hasPrev: current > 1,
    hasNext: current < pages,
    needed: pages > 1,
  };
}

/** Per-country tallies, used by the "is there one near me?" verdict. */
export function countryReport(venues, country) {
  const live = venues.filter((v) => v.removed_at === null);
  const mine = live.filter((v) => v.country === country);
  const film70 = mine.filter((v) => v.has_70mm === 1);
  const region = mine.length ? mine[0].region : null;

  // When the answer is "no", the useful follow-up is where the nearest ones
  // are; region is the only geography this dataset carries.
  const nearby = new Map();
  if (!film70.length && region) {
    for (const v of live) {
      if (v.region === region && v.country !== country && v.has_70mm === 1) {
        nearby.set(v.country, (nearby.get(v.country) || 0) + 1);
      }
    }
  }

  return {
    country,
    region,
    venues: mine.length,
    film70: film70.length,
    dome: mine.filter((v) => v.is_dome === 1).length,
    ar1_43: mine.filter((v) => v.is_1_43 === 1).length,
    nearby: [...nearby.entries()]
      .map(([name, n]) => ({ country: name, n }))
      .sort((a, b) => b.n - a.n || COLLATOR.compare(a.country, b.country)),
  };
}
