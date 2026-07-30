/* Guessing which country the visitor is in, without asking them and without
 * telling anyone else.
 *
 * The published site is a bag of static files, so there is no server to read an
 * IP address and no backend to call a geolocation service.  That turns out to
 * be a feature: the browser already knows its own timezone, and a timezone maps
 * to a country almost as well as an IP does - without a permission prompt,
 * without a network request, and without the visitor's address leaving the
 * machine.
 *
 * Order of preference:
 *   1. an explicit choice the visitor made before (localStorage)
 *   2. IANA timezone -> ISO country code, via the table export.py bakes in
 *   3. the region subtag of the browser's language ("en-IN" -> IN)
 *
 * Anything can be wrong, so the answer is always presented as a guess with a
 * visible override rather than as a fact.
 */

const STORAGE_KEY = "country";

/** The visitor's IANA timezone, or "" if the browser will not say. */
export function currentTimezone() {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || "";
  } catch {
    return "";
  }
}

/** ISO 3166-1 alpha-2 codes implied by the browser's language preferences. */
export function languageRegions() {
  const tags = navigator.languages?.length
    ? navigator.languages
    : [navigator.language].filter(Boolean);

  const codes = [];
  for (const tag of tags) {
    let region = "";
    try {
      region = new Intl.Locale(tag).region || "";
    } catch {
      // Older or malformed tags: fall back to reading the subtag directly.
      const parts = String(tag).split("-");
      region = parts.length > 1 ? parts[1] : "";
    }
    if (/^[A-Za-z]{2}$/.test(region)) codes.push(region.toUpperCase());
  }
  return codes;
}

/**
 * Work out the visitor's country.
 *
 * `geo` is the block export.py writes: { timezones: {zone: CC}, countries:
 * {CC: name} }.  Returns the country name as the dataset spells it when we have
 * venues there, plus how the guess was made and whether it can be trusted.
 */
export function detectCountry(geo, { stored = readStored() } = {}) {
  if (stored) {
    return { country: stored, code: codeFor(geo, stored), source: "chosen",
             confident: true };
  }

  const zone = currentTimezone();
  const zoneCode = geo.timezones?.[zone];
  if (zoneCode) return describe(geo, zoneCode, "timezone", zone);

  for (const code of languageRegions()) {
    const named = geo.countries?.[code];
    if (named) return describe(geo, code, "language", code);
  }
  const [first] = languageRegions();
  if (first) return describe(geo, first, "language", first);

  return { country: "", code: "", source: "unknown", confident: false };
}

function describe(geo, code, source, detail) {
  const known = geo.countries?.[code];
  return {
    // A country we hold venues for gets the dataset's spelling; anywhere else
    // we still name it, so the page can say "nothing listed in Norway" rather
    // than shrugging.
    country: known || regionName(code),
    code,
    source,
    detail,
    listed: Boolean(known),
    confident: source === "timezone",
  };
}

/** Turn an ISO code into an English country name using the browser's own data. */
export function regionName(code) {
  try {
    return new Intl.DisplayNames(["en"], { type: "region" }).of(code) || code;
  } catch {
    return code;
  }
}

function codeFor(geo, country) {
  const entry = Object.entries(geo.countries || {})
    .find(([, name]) => name === country);
  return entry ? entry[0] : "";
}

// ------------------------------------------------------------- persistence

export function readStored() {
  try {
    return localStorage.getItem(STORAGE_KEY) || "";
  } catch {
    return ""; // private browsing, storage disabled - not worth failing over
  }
}

export function storeCountry(country) {
  try {
    if (country) localStorage.setItem(STORAGE_KEY, country);
    else localStorage.removeItem(STORAGE_KEY);
  } catch {
    /* ignore */
  }
}

/** Plain-English account of how the guess was made, for the UI to show. */
export function explain(detection) {
  switch (detection.source) {
    case "chosen":
      return "You picked this country.";
    case "timezone":
      return `Guessed from your timezone (${detection.detail}). Nothing left your browser.`;
    case "language":
      return `Guessed from your browser's language settings (${detection.detail}).`;
    default:
      return "Couldn't guess where you are — pick a country.";
  }
}
