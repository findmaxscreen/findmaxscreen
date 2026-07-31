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

// ------------------------------------------------------- precise position

/* Everything above guesses a country from what the browser already knows, and
 * costs the visitor nothing.  What follows is the other kind: an actual fix
 * from navigator.geolocation, which needs permission and is therefore only ever
 * started by a click.
 *
 * The position is deliberately never persisted.  A country is coarse and was
 * chosen deliberately, so localStorage is fair; a lat/lon is neither. Once
 * permission is granted the browser remembers it, and maximumAge makes the
 * second call effectively free - so storing it would buy nothing anyway.
 */

/**
 * True when a fix is even possible.
 *
 * The secure-context check is not belt and braces: `navigator.geolocation`
 * exists on a plain-HTTP origin too, and calling it there fails with
 * PERMISSION_DENIED without ever showing a prompt. Testing over a LAN address
 * would otherwise offer a button that cannot work and blame the visitor for
 * refusing. localhost counts as secure, so local development is unaffected.
 */
export function canLocate() {
  return typeof navigator !== "undefined"
    && "geolocation" in navigator
    && typeof window !== "undefined"
    && window.isSecureContext;
}

/**
 * What the browser will do if we ask, without asking: "granted" means a call
 * raises no prompt, "prompt" means it would, "denied" means it cannot succeed.
 * "unknown" when the Permissions API is unavailable - Safari has been late
 * here - in which case treat asking as a prompt.
 */
export async function permissionState() {
  try {
    const status = await navigator.permissions.query({ name: "geolocation" });
    return status.state;
  } catch {
    return "unknown";
  }
}

/**
 * Ask for a position. Resolves {lat, lon, accuracy}; rejects with a code of
 * "denied", "unavailable" or "timeout" so the caller can say something useful.
 *
 * High accuracy is off on purpose: ranking cinemas needs a town, not a doorway,
 * and enabling it can spin up GPS for tens of seconds on a phone. A five-minute
 * cached fix is accepted for the same reason.
 */
export function locate({ timeout = 8000, maximumAge = 300000 } = {}) {
  return new Promise((resolve, reject) => {
    if (!canLocate()) {
      reject(Object.assign(new Error("no geolocation"), { code: "unsupported" }));
      return;
    }
    navigator.geolocation.getCurrentPosition(
      (pos) => resolve({
        lat: pos.coords.latitude,
        lon: pos.coords.longitude,
        accuracy: pos.coords.accuracy,
      }),
      (err) => {
        const code = err.code === 1 ? "denied"
          : err.code === 3 ? "timeout"
          : "unavailable";
        reject(Object.assign(new Error(err.message || code), { code }));
      },
      { enableHighAccuracy: false, timeout, maximumAge },
    );
  });
}

/**
 * What to tell the visitor about the precise-location attempt.
 *
 * Note this never claims "nothing left your browser" the way the timezone guess
 * does. Resolving a position usually means the browser consulting its vendor's
 * location service, and while that is the browser's business rather than ours,
 * saying otherwise would be a lie by omission.
 */
export function explainPosition(status, accuracy = 0) {
  switch (status) {
    case "locating":
      return "Getting your location…";
    case "located": {
      const km = Math.round((accuracy || 0) / 1000);
      const how = km > 1 ? ` Accurate to about ${km} km.` : "";
      return `Sorted from your location.${how} It stays in this tab — nothing is stored or sent on.`;
    }
    case "denied":
      return "Location permission was refused, so the list is ordered by country instead. "
        + "Your browser's site settings can undo that.";
    case "timeout":
      return "Your device took too long to find a position. Sorted by country instead.";
    // POSITION_UNAVAILABLE. The browser tried and the device had nothing to
    // give, which is not the same as the browser refusing - and on a Mac it is
    // almost always one switch in System Settings rather than anything about
    // this site. Saying "this browser won't" sent people looking in the wrong
    // place entirely.
    case "unavailable":
      return "Your device couldn't work out where it is — on a Mac that is usually "
        + "Location Services being off for your browser, under System Settings › "
        + "Privacy & Security. Sorted by country instead.";
    case "unsupported":
      return "This browser can't share a location here. Sorted by country instead.";
    default:
      return "";
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
