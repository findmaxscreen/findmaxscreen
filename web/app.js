/* FindMaxScreen — the public page.
 *
 * Static: one fetch of data/venues.json on load, and everything after that
 * happens in the browser.  There is no API and no admin surface here; the
 * refresh tooling lives in admin.html, which is never published.
 */

import {
  index, search, countryReport, paginate, cityIndex, fold, haversineKm,
} from "./query.js";
import {
  detectCountry, storeCountry, explain,
  canLocate, locate, permissionState, explainPosition,
  storeCity, readStoredCity,
} from "./geo.js";

const $ = (id) => document.getElementById(id);

const CONTROLS = {
  q: $("q"),
  film70: $("film70"),
  dome: $("dome"),
  commercial: $("commercial"),
  include_removed: $("include_removed"),
  country: $("country"),
  projector: $("projector"),
  film: $("film"),
  ar: $("ar"),
  sort: $("sort"),
};

const results = $("results");
const countEl = $("count");
const banner = $("banner");

const TABS = ["all", "nearme", "types"];

/* Links shared before the sections were reorganised carry these. "country" was
 * the tab that answered both "what is in my country" and, briefly and badly,
 * "what is near me"; the second is what its visitors were after, and the first
 * now lives in a section of All venues that they will scroll straight past. */
const TAB_ALIASES = { country: "nearme", film70: "all" };

/* Every way a position request can end badly. Kept in one place because the
 * callers below all have to agree about what "it failed" means. */
const GEO_FAILURES = ["denied", "unavailable", "timeout", "unsupported"];

/* The subset worth giving up on.
 *
 * A refusal is remembered by the browser and a missing API will not appear, so
 * asking again shows the visitor nothing and spins forever. The other two are
 * weather: POSITION_UNAVAILABLE is what a Mac returns when Wi-Fi scanning
 * momentarily has nothing to go on, and a timeout is just a slow fix. Treating
 * those as final meant one hiccup jammed the section for the rest of the visit
 * and only a hunt for the Try again button got it back - and since permission
 * has already been granted by then, re-asking costs no prompt at all. */
const GEO_FINAL = ["denied", "unsupported"];

/* The filter toggles are buttons, not checkboxes, so `checked` is not a thing
 * they have. Everything reads and writes their state through these two rather
 * than reaching for the attribute, which is how the old `.checked` calls stayed
 * consistent across eight call sites. */
const isOn = (el) => el.getAttribute("aria-checked") === "true";
const setOn = (el, on) => el.setAttribute("aria-checked", String(Boolean(on)));

const TOGGLES = ["film70", "dome", "commercial", "include_removed"];

// 476 rows in one scroll is a wall. Enough per page to be worth a scroll,
// few enough that the end is always in sight.
const PAGE_SIZE = 25;

const state = {
  data: null,
  venues: [],
  tab: "all",
  detection: null,
  country: "",
  // Set only by the 70 mm call to action; there is no region control in the
  // filter bar, so it surfaces as a removable chip instead.
  region: "",
  page: 1,
  /* True while the country filter holds the guess rather than a choice.
   *
   * It is what keeps the front page's URL empty: an automatic country is not
   * something the visitor did, so it does not belong in a link. The corollary
   * is that a URL with no `country` gets the default applied on arrival, which
   * is why clearing the filter deliberately writes `country=any` - otherwise a
   * reload of "everywhere" would come back scoped to wherever you are. */
  countryAuto: false,
  // A precise fix, once the visitor has offered one. Memory only, never
  // localStorage - see the note in geo.js about why a lat/lon is not a country.
  origin: null,
  // Set when the origin is a city the visitor named rather than a measured
  // position; geoStatus is "manual" whenever this is the origin in use.
  originCity: "",
  cities: null,
  geoStatus: "idle",
  geoAccuracy: 0,
};

/* The wiki stores terse family codes.  Spelling them out - and saying what they
 * actually are - is the difference between a filter only an enthusiast can read
 * and one anybody can.  Presentation only; the data keeps the short codes. */
const FILM_LABELS = {
  "GT3D": ["IMAX GT 3D", "Dual 15/70 mm projectors running a 3D print. The flagship film system."],
  "GT": ["IMAX GT", "The original 15/70 mm grand-theatre projector."],
  "GT Dome": ["IMAX GT Dome", "15/70 mm projected onto a tilted dome."],
  "SR": ["IMAX SR", "The smaller-venue 15/70 mm projector."],
  "SR Dome": ["IMAX SR Dome", "15/70 mm onto a dome, in a smaller house."],
  "Dome": ["IMAX Dome", "Film projected onto a dome screen."],
  "15/70": ["Model unspecified", "The wiki lists a 15/70 mm projector but not which model."],
};

const DIGITAL_LABELS = {
  "GT Laser": ["IMAX GT Laser", "The dual-4K laser system built for the biggest 1.43:1 screens."],
  "CoLa": ["IMAX CoLa", "IMAX's single-projector 'commercial laser', the most widely installed system."],
  "XT": ["IMAX Laser XT", "A laser retrofit for mid-size auditoriums."],
  "Dome Laser": ["IMAX Dome Laser", "Laser projection onto a dome."],
  "Digital": ["IMAX Digital", "The older 2K xenon dual-projector system."],
  "Laser": ["IMAX with Laser", "Laser projection, model unstated."],
};

const label = (map, key) => (map[key] || [key, ""])[0];
const blurb = (map, key) => (map[key] || [key, ""])[1];

const ANY_FILM = "__any70__";
const fmt = new Intl.NumberFormat();
const SVG_NS = "http://www.w3.org/2000/svg";

// --------------------------------------------------------------- utilities

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function icon(name) {
  const svg = document.createElementNS(SVG_NS, "svg");
  svg.setAttribute("class", "icon");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("aria-hidden", "true");
  const use = document.createElementNS(SVG_NS, "use");
  use.setAttribute("href", `#i-${name}`);
  svg.append(use);
  return svg;
}

function debounce(fn, ms) {
  let timer;
  return (...args) => {
    clearTimeout(timer);
    timer = setTimeout(() => fn(...args), ms);
  };
}

const plural = (n, word) => `${fmt.format(n)} ${word}${n === 1 ? "" : "s"}`;

// ----------------------------------------------------------------- queries

/* The tab sets the scope; the controls refine within it.  Each tab also hides
 * the control it subsumes, so the section heading and the filter bar can never
 * claim different things. */
function criteria() {
  const c = {
    q: CONTROLS.q.value.trim(),
    dome: isOn(CONTROLS.dome),
    commercial: isOn(CONTROLS.commercial),
    includeRemoved: isOn(CONTROLS.include_removed),
    projector: CONTROLS.projector.value,
    film: CONTROLS.film.value,
    ar: CONTROLS.ar.value,
    sort: CONTROLS.sort.value || "location",
    film70: isOn(CONTROLS.film70),
    country: CONTROLS.country.value,
    region: state.region,
    origin: state.origin,
  };
  // Near me is a worldwide question. The country filter is hidden while the
  // section is open and dropped from the criteria here, because pinning it
  // would hide the cinema an hour away over a border - and in a country the
  // size of India it is what put Delhi in front of someone in Mumbai.
  if (state.tab === "nearme") {
    c.country = "";
    c.region = "";
    c.sort = state.origin ? "distance" : "location";
  }
  // A sort we cannot honour is not left standing: a shared ?sort=distance link
  // arrives before any permission exists, and a refusal leaves the choice on
  // the control until requestPosition() resets it.
  if (c.sort === "distance" && !c.origin) c.sort = "location";
  return c;
}

/* Every filter currently narrowing the list, each with the means to drop it.
 * A row of chips beats a lone "Clear" button: it says what is on as well as
 * offering to turn it off. Filters a section already implies are excluded -
 * you cannot switch off the thing that defines the section you are in. */
function activeFilters() {
  const chips = [];
  const add = (label, clear) => chips.push({ label, clear });

  if (CONTROLS.q.value.trim()) {
    add(`“${CONTROLS.q.value.trim()}”`, () => { CONTROLS.q.value = ""; });
  }
  if (isOn(CONTROLS.film70)) {
    add("70 mm film", () => setOn(CONTROLS.film70, false));
  }
  if (isOn(CONTROLS.dome)) add("Dome", () => setOn(CONTROLS.dome, false));
  if (isOn(CONTROLS.commercial)) {
    add("Commercial films", () => setOn(CONTROLS.commercial, false));
  }
  if (isOn(CONTROLS.include_removed)) {
    add("Including closed", () => setOn(CONTROLS.include_removed, false));
  }
  // Near me ignores the country filter outright, so offering to clear it there
  // would be offering to switch off something that is already off.
  if (state.tab !== "nearme" && CONTROLS.country.value) {
    add(CONTROLS.country.value, () => {
      CONTROLS.country.value = "";
      state.countryAuto = false;
    });
  }
  if (state.region) add(state.region, () => { state.region = ""; });
  if (CONTROLS.projector.value) {
    add(label(DIGITAL_LABELS, CONTROLS.projector.value),
        () => { CONTROLS.projector.value = ""; });
  }
  if (CONTROLS.film.value) {
    add(label(FILM_LABELS, CONTROLS.film.value), () => { CONTROLS.film.value = ""; });
  }
  if (CONTROLS.ar.value) {
    add(`${CONTROLS.ar.value}:1`, () => { CONTROLS.ar.value = ""; });
  }
  return chips;
}

/* Only the controls that live inside the collapsed panel. The search box, the
 * region chip and now the two quick filters sit outside it and are already
 * visible, so opening the panel for them would point at nothing. Sort is
 * excluded too - it reorders the list without hiding anything, so a shut panel
 * never misrepresents the count. */
function panelFiltersActive() {
  return ["dome", "commercial", "include_removed"].some((key) => isOn(CONTROLS[key]))
      || ["projector", "film", "ar"].some((key) => CONTROLS[key].value !== "");
}

function resetFilters() {
  CONTROLS.q.value = "";
  for (const key of TOGGLES) setOn(CONTROLS[key], false);
  for (const key of ["country", "projector", "film", "ar"]) CONTROLS[key].value = "";
  CONTROLS.sort.value = "location";
  // Clearing the country here is a deliberate widening to everywhere, not a
  // return to the guess. Only the masthead does the latter.
  state.countryAuto = false;
  state.region = "";
}

function renderActiveFilters() {
  const chips = activeFilters();
  const bar = $("activefilters");
  bar.hidden = chips.length === 0;

  const box = $("af-chips");
  box.replaceChildren();
  for (const chip of chips) {
    const button = el("button", "chip");
    button.type = "button";
    button.append(document.createTextNode(chip.label), icon("cross"));
    button.title = `Remove this filter`;
    button.addEventListener("click", () => { chip.clear(); update(); });
    box.append(button);
  }
}

function syncUrlBar() {
  const params = new URLSearchParams();
  if (state.tab !== "all") params.set("tab", state.tab);
  const c = criteria();
  if (c.q) params.set("q", c.q);
  for (const [key, on] of [["dome", c.dome], ["commercial", c.commercial],
                           ["include_removed", c.includeRemoved]]) {
    if (on) params.set(key, "1");
  }
  if (c.film70) params.set("film70", "1");
  // Read from the control, not the criteria: Near me blanks the country in
  // criteria() to unpin the search, and a link that dropped it would come back
  // with the visitor's country filter silently cleared.
  //
  // A guessed country is left out entirely - that is what makes the front page
  // "/" and not "/?country=India" - while a deliberate "everywhere" is written
  // as `any`, because the absence of the parameter already means "guess".
  if (!state.countryAuto) {
    params.set("country", CONTROLS.country.value || "any");
  }
  for (const key of ["projector", "film", "ar"]) {
    if (CONTROLS[key].value) params.set(key, CONTROLS[key].value);
  }
  if (CONTROLS.sort.value !== "location") params.set("sort", CONTROLS.sort.value);
  if (state.region) params.set("region", state.region);
  if (state.page > 1) params.set("page", String(state.page));
  const query = params.toString();
  history.replaceState(null, "", query ? `?${query}` : location.pathname);
}

// --------------------------------------------------------------- rendering

function badge(text, cls, title, iconName) {
  const node = el("span", cls ? `badge ${cls}` : "badge");
  if (iconName) node.append(icon(iconName));
  node.append(document.createTextNode(text));
  if (title) node.title = title;
  return node;
}

/* Outbound links for one venue.
 *
 * Coordinates come from OpenStreetMap, but the links go to Google Maps: a Maps
 * URL is a plain hyperlink, needing no key and no API, and it is where people
 * already keep their reviews and navigation. Nothing Google-derived is stored -
 * only OSM coordinates are, which is what keeps the licensing clean.
 *
 * Showtimes are not hosted here. They are licensed, per-theatre and change
 * daily, so a static site cannot hold them honestly; a search link always
 * resolves to whatever is current and works in all 56 countries. */
function venueLinks(v) {
  const place = [v.name, v.city, v.state, v.country].filter(Boolean).join(", ");
  const links = [];

  // Rebuilt here rather than shipped: the same string for all 476 venues cost
  // 55 KB of the payload, and it is pure function of fields already present.
  const search = [v.name, v.city, v.country].filter(Boolean).join(", ");
  // One Google Maps link, not two: the place card already carries a Directions
  // button, so a separate one was a second route to the same screen. The
  // coordinates still earn their keep here - an exact fix opens the theatre
  // itself rather than a name search that may land on the wrong branch.
  const precise = v.lat != null && v.lon != null && v.geo_precision === "venue";
  links.push([
    precise
      ? `https://www.google.com/maps/search/?api=1&query=${v.lat},${v.lon}`
      : `https://www.google.com/maps/search/?api=1&query=${encodeURIComponent(search)}`,
    "Maps", "map",
    precise
      ? "Open in Google Maps — directions, reviews and photos are on the place card."
      : `Open in Google Maps. This one isn't precisely mapped, so Google resolves ${v.city} from the name.`,
  ]);

  // The venue's own site, from OpenStreetMap's website tag. Usually the page
  // that actually sells the tickets - and unlike a search result, ODbL lets us
  // store and republish it.
  if (v.website) {
    links.push([v.website, "Cinema site", "globe",
                "The theatre's own website — usually where the showtimes are."]);
  }

  links.push([
    `https://www.google.com/search?q=${encodeURIComponent(place + " showtimes")}`,
    "Showtimes", "ticket",
    "Search for current showtimes at this theatre.",
  ]);

  return links;
}

function venueCard(v) {
  const card = el("article", "venue");
  if (v.has_70mm) card.classList.add("film70");
  if (v.removed_at) card.classList.add("gone");

  // The facts go in one box and the actions in another, so the card is a grid
  // of exactly two cells. Letting the four fact elements be grid items in their
  // own right is what caused the trouble before: the link stack landed in the
  // title's row and stretched it.
  const main = el("div", "venue-main");
  card.append(main);
  main.append(el("h3", null, v.name));

  const where = el("p", "where");
  where.append(icon("pin"), document.createTextNode(
    [v.city, v.state, v.country].filter(Boolean).join(", ")));
  // Only present when the list is ordered by distance, and rounded hard: half
  // these venues are located to their city, so a decimal would be a precision
  // the data does not have.
  if (v._km != null) {
    // "0 km" reads as missing data rather than as "very close", and it is what
    // a city-mapped venue in the town you are standing in rounds to.
    const km = v._km < 1 ? "< 1 km"
      : `${fmt.format(v._km < 100 ? Math.round(v._km) : Math.round(v._km / 10) * 10)} km`;
    const tag = el("span", "away", km);
    tag.title = v.geo_precision === "venue"
      ? "Straight-line distance from where you are to the theatre."
      : `Straight-line distance from where you are to ${v.city} — this one is only mapped to its city.`;
    where.append(tag);
  }
  main.append(where);

  const badges = el("div", "badges");
  if (v.has_70mm) {
    badges.append(badge(label(FILM_LABELS, v.film_family), "film",
      blurb(FILM_LABELS, v.film_family) + " Real 15/70 mm film, not digital.",
      "film"));
  }
  if (v.is_1_43) {
    badges.append(badge("1.43:1", "tall",
      "The full-height IMAX frame — the tallest picture the format offers.", "tall"));
  }
  if (v.is_dome) {
    badges.append(badge("Dome", "dome", "Projected onto a tilted dome.", "dome"));
  }
  if (v.projector_family) {
    badges.append(badge(label(DIGITAL_LABELS, v.projector_family), "digital",
      blurb(DIGITAL_LABELS, v.projector_family), "projector"));
  }
  if (v.screen_w_m && v.screen_h_m) {
    badges.append(badge(`${v.screen_w_m} × ${v.screen_h_m} m`, "size",
      v.screen_area_m2 ? `${fmt.format(v.screen_area_m2)} m² of screen` : "", "frame"));
  } else if (v.screen_area_m2) {
    badges.append(badge(`${fmt.format(v.screen_area_m2)} m²`, "size", "", "frame"));
  }
  if (v.commercial_films === 0) {
    badges.append(badge("Museum programme", "muted",
      "Documentaries and educational films rather than commercial releases.", "museum"));
  }
  if (v.is_temporary) {
    badges.append(badge("Temporary", "muted", "A temporary installation.", "clock"));
  }
  if (v.removed_at) {
    badges.append(badge("De-listed", "muted",
      "No longer on the wiki's list. Kept here so the history survives.", "archive"));
  }
  if (badges.children.length) main.append(badges);

  if (v.data_notes) main.append(el("p", "note", v.data_notes));

  const links = el("div", "links");
  for (const [href, text, iconName, title] of venueLinks(v)) {
    const a = el("a");
    a.href = href;
    // mailto: must not open a tab that then sits there empty.
    if (!href.startsWith("mailto:")) {
      a.target = "_blank";
      a.rel = "noreferrer";
    }
    a.title = title;
    a.append(icon(iconName), document.createTextNode(text));
    links.append(a);
  }
  card.append(links);

  return card;
}

function summarize(result, c) {
  if (!result.total) return "";
  const noun = plural(result.total, "theatre");
  if (c.film70) return `${noun} still threading 15/70 mm film.`;
  if (result.shown < result.total) return `${noun} — showing the first ${fmt.format(result.shown)}.`;
  const film70 = result.venues.filter((v) => v.has_70mm).length;
  return film70 ? `${noun}, ${fmt.format(film70)} of them running real film.` : `${noun}.`;
}

// ------------------------------------------------------------ the sections

/* The country the My Country section reports on.
 *
 * The promoted filter wins when it is set, so changing it moves the section
 * with the list instead of leaving the two describing different countries.
 * With the filter on Anywhere it falls back to the guess, which is what a
 * visitor who has touched nothing should see. */
function shownCountry() {
  return CONTROLS.country.value || state.country;
}

/* My Country: one section answering both country questions.
 *
 * These were two tabs. "Is there real film here?" is the question this site was
 * built for and led the 70 mm section; "how many are there?" opened the country
 * section. Asked one after the other about the same place they are obviously
 * one thing, and splitting them across two tabs meant reading a country's
 * headline twice to get both halves.
 *
 * Order is question, answer, detail, then the wider count: the 15/70 verdict
 * leads because it is the one that decides whether to get on a plane. */
function myCountryBanner() {
  const country = shownCountry();
  const box = el("div", "verdict");

  if (!country) {
    box.append(el("p", "verdict-q", "Where are you?"));
    box.append(el("p", null,
      "Pick a country above and this will tell you what is there, and whether "
      + "any of it runs real 15/70 mm film."));
    return box;
  }

  const report = countryReport(state.venues, country);
  const yes = report.film70 > 0;
  box.classList.add(yes ? "yes" : "no");
  box.append(el("p", "verdict-q", `Is there 15/70 mm film IMAX in ${country}?`));

  const answer = el("p", "verdict-a");
  answer.append(icon(yes ? "film" : "cross"));
  answer.append(document.createTextNode(yes ? "Yes" : "No"));
  box.append(answer);

  if (yes) {
    box.append(el("p", null,
      `${plural(report.film70, "theatre")} in ${country} still `
      + `${report.film70 === 1 ? "runs" : "run"} real film.`));
  } else if (report.nearby.length) {
    const nearest = report.nearby.slice(0, 4)
      .map((c) => `${c.country} (${c.n})`).join(", ");
    box.append(el("p", null,
      `Nothing in ${country}. The closest in ${report.region}: ${nearest}.`));
  } else {
    box.append(el("p", null,
      `No 15/70 mm film IMAX in ${country}, and none elsewhere in `
      + `${report.region || "the region"} either.`));
  }

  // The wider count, which used to be the whole of the country section.
  if (!report.venues) {
    box.append(el("p", null,
      `No IMAX venues listed in ${country} at all. The wiki only lists laser `
      + "and 15/70 mm houses, so a plain digital screen may still exist there."));
  } else {
    const parts = [plural(report.venues, "IMAX venue")];
    if (report.ar1_43) parts.push(`${report.ar1_43} with a full-height 1.43:1 screen`);
    if (report.dome) parts.push(`${report.dome} dome`);
    box.append(el("p", null, parts.join(" · ")));
  }

  // Only when the country on screen is the one we worked out. Once the filter
  // has been pointed somewhere else, "guessed from your timezone" would be
  // explaining a guess that is no longer on display.
  if (country === state.country) {
    const how = el("p", "how");
    how.append(document.createTextNode(explain(state.detection || {})));
    box.append(how);
  }

  const cta = film70Cta(report, country);
  if (cta) box.append(cta);
  return box;
}

/* The list under the verdict is all 58 worldwide, which is rarely what someone
 * who just read "yes, there is one in your country" wants next.  Offer the
 * obvious narrowing as a button rather than making them work out which of the
 * controls below does it. */
function film70Cta(report, country) {
  const narrowedToCountry = CONTROLS.country.value === country
    && isOn(CONTROLS.film70);
  const narrowedToRegion = state.region === report.region;

  let question, action, apply;

  if (narrowedToCountry || narrowedToRegion) {
    question = "Showing a narrowed list.";
    action = "Show all 58 worldwide";
    apply = () => {
      CONTROLS.country.value = "";
      state.countryAuto = false;
      state.region = "";
      setOn(CONTROLS.film70, true);
    };
  } else if (report.film70 > 0) {
    question = "Want to see just those?";
    action = report.film70 === 1
      ? `Show the one in ${country}`
      : `Show all ${fmt.format(report.film70)} in ${country}`;
    // The 70 mm toggle is the quick filter now, so the button that used to set
    // only the country sets both - otherwise "show the 3 in India" handed back
    // all 13 Indian venues and left the reader to find the toggle.
    apply = () => {
      CONTROLS.country.value = country;
      state.countryAuto = false;
      state.region = "";
      setOn(CONTROLS.film70, true);
    };
  } else if (report.nearby.length) {
    const total = report.nearby.reduce((sum, c) => sum + c.n, 0);
    question = "Want to see the closest ones?";
    action = total === 1
      ? `Show the one in ${report.region}`
      : `Show all ${fmt.format(total)} in ${report.region}`;
    apply = () => {
      state.region = report.region;
      CONTROLS.country.value = "";
      state.countryAuto = false;
      setOn(CONTROLS.film70, true);
    };
  } else {
    return null;
  }

  const wrap = el("div", "cta");
  wrap.append(el("span", "cta-q", question));
  const button = el("button", "ghost small");
  button.type = "button";
  button.append(icon("film"), document.createTextNode(action));
  button.addEventListener("click", () => { apply(); update(); });
  wrap.append(button);
  return wrap;
}

/* Near me: distance, and nothing else.
 *
 * This section used to be the country section wearing a different label, which
 * is how it came to show an alphabetical list of Indian venues - Delhi first,
 * because D sorts before M - to a reader standing in Mumbai. It now has exactly
 * one job, and when it cannot do that job it says so instead of quietly
 * offering the alphabet as though it were geography.
 */
function nearMeBanner() {
  const box = el("div", "verdict");

  if (state.origin) {
    box.classList.add("yes");
    if (state.geoStatus === "manual") {
      box.append(el("p", "verdict-q", `Nearest to ${state.originCity}`));
      box.append(el("p", null,
        "Distances are measured from the middle of the city you named, as "
        + "straight lines. Name a different one below if that is wrong."));
      box.append(cityPicker());
      if (canLocate()) box.append(preciseCta());
    } else {
      box.append(el("p", "verdict-q", "Nearest first"));
      box.append(el("p", null,
        "Every venue on earth, closest to you at the top. Distances are "
        + "straight lines, and about half these theatres are mapped to their "
        + "city rather than their door."));
      // A measured fix lives only in memory, so a reload on a machine whose
      // provider fails cold loses the ranking entirely. Offering to remember
      // the nearest city converts detection into declaration - the visitor
      // says it, so it may be stored - and a reload then opens ranked from
      // that city while a fresh fix is tried quietly behind it.
      const near = nearestCity();
      if (near && !readStoredCity()) {
        const wrap = el("div", "cta");
        wrap.append(el("span", "cta-q", "Keep this working after a reload?"));
        const b = el("button", "ghost small");
        b.type = "button";
        b.append(icon("pin"), document.createTextNode(`Remember I'm near ${near}`));
        b.addEventListener("click", () => {
          storeCity(near);
          state.originCity = near;
          update();
        });
        wrap.append(b);
        box.append(wrap);
      }
    }
    return box;
  }

  if (!canLocate()) {
    box.append(el("p", "verdict-q", "This browser won't share a location"));
    box.append(el("p", null,
      "Ranking by distance needs to know where you are - but you can just "
      + "say so instead."));
    box.append(cityPicker());
    return box;
  }

  const failed = GEO_FAILURES.includes(state.geoStatus);
  if (failed) box.classList.add("no");
  box.append(el("p", "verdict-q",
    state.geoStatus === "locating" ? "Finding you…" : "Where are you?"));
  box.append(el("p", null, failed
    ? "Without a location this section has nothing to rank, so the list below "
      + "is in country order — not distance order."
    : "This section ranks all 476 venues by how far they are from you. Your "
      + "browser will ask before sharing anything."));

  // Which failure, said here rather than only in the line under the result
  // count. That line sits below the search box and the filters, far enough from
  // this box that "it didn't work" and "here is why" read as unrelated - and
  // the why is the difference between checking a setting and giving up.
  if (failed) {
    box.append(el("p", "how", explainPosition(state.geoStatus, state.geoAccuracy)));
  }

  if (failed && state.geoStatus !== "unsupported") {
    const wrap = el("div", "cta");
    wrap.append(el("span", "cta-q",
      state.geoStatus === "denied" ? "Changed your mind?" : "Worth another go?"));
    const button = el("button", "ghost small");
    button.type = "button";
    button.append(icon("pin"), document.createTextNode("Try again"));
    button.addEventListener("click", () => { state.geoStatus = "idle"; requestPosition(); });
    wrap.append(button);
    box.append(wrap);
  }

  // The device is not the only thing that knows where the visitor is - the
  // visitor does. Offered in every state short of an answered one, because a
  // machine that cannot produce a fix (an access point Apple has never heard
  // of, say) fails identically forever, and a dead end here means the whole
  // section never works for that person at all.
  box.append(cityPicker());
  return box;
}

/* A typeahead over every city the data can measure from. A named city becomes
 * the origin exactly as a fix would, at the only precision the ranking ever
 * claims - half the venues are mapped to their city centre anyway.
 *
 * The menu is drawn by hand rather than with <datalist>: the native popover is
 * browser chrome, takes no CSS at all, and sat inside the newsprint page like a
 * sticker on a broadsheet. Matching happens through fold(), so "sao" finds
 * S\u00e3o Paulo the same way the search box would. */
function cityPicker() {
  const wrap = el("div", "cta");
  const lbl = el("label", "cta-q");
  lbl.append(document.createTextNode("Or name your city:\u00a0"));
  const box = el("span", "citybox");
  const input = el("input", "citypick");
  input.type = "text";
  input.placeholder = "Start typing\u2026";
  input.autocomplete = "off";
  input.setAttribute("role", "combobox");
  input.setAttribute("aria-expanded", "false");
  const menu = el("ul", "citymenu");
  menu.hidden = true;

  const close = () => {
    menu.hidden = true;
    input.setAttribute("aria-expanded", "false");
  };

  input.addEventListener("input", () => {
    const q = fold(input.value.trim());
    if (!q) { close(); return; }
    const rows = [];
    for (const name of state.cities.keys()) {
      if (fold(name).includes(q)) {
        rows.push(name);
        if (rows.length === 8) break;
      }
    }
    menu.replaceChildren(...rows.map((name) => {
      const li = el("li");
      const b = el("button", null, name);
      b.type = "button";
      // mousedown, not click: it fires before the input's blur closes the
      // menu, so the choice lands instead of evaporating.
      b.addEventListener("mousedown", (e) => { e.preventDefault(); setManualOrigin(name); });
      li.append(b);
      return li;
    }));
    menu.hidden = rows.length === 0;
    input.setAttribute("aria-expanded", String(!menu.hidden));
  });

  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter") {
      e.preventDefault();
      const first = menu.querySelector("button");
      if (first) setManualOrigin(first.textContent);
      else setManualOrigin(input.value.trim());
    } else if (e.key === "Escape") close();
  });
  input.addEventListener("blur", close);

  box.append(input, menu);
  lbl.append(box);
  wrap.append(lbl);
  return wrap;
}

/* The city the fix landed nearest to, for offering to remember. 150 km keeps
 * the offer honest: an origin in the middle of nowhere names nothing. */
function nearestCity() {
  if (!state.origin || !state.cities) return "";
  let best = "";
  let bestKm = Infinity;
  for (const [name, c] of state.cities) {
    const km = haversineKm(state.origin.lat, state.origin.lon, c.lat, c.lon);
    if (km < bestKm) { bestKm = km; best = name; }
  }
  return bestKm <= 150 ? best : "";
}

function preciseCta() {
  const wrap = el("div", "cta");
  wrap.append(el("span", "cta-q", "Got a location after all?"));
  const button = el("button", "ghost small");
  button.type = "button";
  button.append(icon("pin"), document.createTextNode("Use my actual position"));
  button.addEventListener("click", () => { state.geoStatus = "idle"; requestPosition(); });
  wrap.append(button);
  return wrap;
}

/* Make a named city the origin. Returns false when the name matches nothing,
 * so the caller can leave the input standing for another go. */
function setManualOrigin(label) {
  if (!label || !state.cities) return false;
  let hit = state.cities.get(label);
  if (!hit) {
    // The datalist suggests exact labels, but typed input deserves a forgiving
    // match: "mumbai" should find "Mumbai, India".
    const folded = label.toLowerCase();
    for (const [name, coords] of state.cities) {
      if (name.toLowerCase().startsWith(folded)) { label = name; hit = coords; break; }
    }
  }
  if (!hit) return false;
  state.origin = { lat: hit.lat, lon: hit.lon };
  state.originCity = label;
  storeCity(label);
  setGeoStatus("manual");
  update();
  return true;
}

function renderBanner() {
  banner.replaceChildren();
  if (state.tab === "all") banner.append(myCountryBanner());
  else if (state.tab === "nearme") banner.append(nearMeBanner());
}

/* Built fresh each render into both containers.  A pager only at the foot of
 * the list is a pager nobody finds - it sits 50 rows down - so the same
 * controls appear beside the result count as well. */
function pagerControls(slice, total) {
  const nav = el("nav", "pager");
  nav.setAttribute("aria-label", "Pagination");

  const step = (label, page, enabled) => {
    const button = el("button", "ghost small", label);
    button.type = "button";
    button.disabled = !enabled;
    button.addEventListener("click", () => goToPage(page));
    return button;
  };

  nav.append(step("Previous", slice.page - 1, slice.hasPrev));
  nav.append(el("span", "pageinfo",
    `${fmt.format(slice.from + 1)}–${fmt.format(slice.to)} of ${fmt.format(total)}`));
  nav.append(step("Next", slice.page + 1, slice.hasNext));
  return nav;
}

function renderPagers(slice, total) {
  for (const id of ["pager-top", "pager-bottom"]) {
    const box = $(id);
    box.replaceChildren();
    if (slice && slice.needed) box.append(pagerControls(slice, total));
  }
}

function render() {
  const c = criteria();
  const result = search(state.venues, c);

  results.replaceChildren();
  if (!result.venues.length) {
    const empty = el("div", "empty");
    empty.append(el("p", "big", "Nothing matches all of that."));
    empty.append(el("p", null,
      "IMAX is a short list to begin with — try dropping a filter."));
    results.append(empty);
    countEl.textContent = "";
    renderPagers(null, 0);
    return;
  }

  // Clamped rather than trusted: a filter change can shrink the result set
  // below the page you were on.
  const slice = paginate(result.total, state.page, PAGE_SIZE);
  state.page = slice.page;
  const page = result.venues.slice(slice.from, slice.to);

  // Country headings only make sense when the sort is by location, and are
  // noise inside a single-country section.
  const grouped = c.sort === "location" && state.tab !== "country";
  if (grouped) {
    let currentKey = null;
    let section = null;
    for (const v of page) {
      const key = [v.country, v.state].filter(Boolean).join(" · ");
      if (key !== currentKey) {
        currentKey = key;
        section = el("section", "group");
        section.append(el("h2", null, key || "Unknown"));
        results.append(section);
      }
      section.append(venueCard(v));
    }
  } else {
    for (const v of page) results.append(venueCard(v));
  }

  countEl.textContent = summarize(result, c);
  renderPagers(slice, result.total);
}

/* Any change to what is being filtered sends you back to page one; only the
 * pager itself moves between pages. */
function update({ keepPage = false } = {}) {
  if (!keepPage) state.page = 1;
  syncUrlBar();
  if (state.tab === "types") {
    banner.replaceChildren();
    $("activefilters").hidden = true;
    return;
  }
  renderActiveFilters();
  renderBanner();
  renderGeoStatus();
  render();
}

function goToPage(page) {
  state.page = page;
  update({ keepPage: true });
  document.getElementById("controls").scrollIntoView({ block: "nearest" });
}

// ------------------------------------------------------------------- tabs

/**
 * `gesture` is true only when a human clicked the tab, and is what makes it
 * safe for "Near me" to ask for a position: the click is on a control that
 * names what the position is for, which is consent by any reasonable reading.
 * A ?tab= link restoring on load passes it false and must stay silent - that is
 * a page the visitor has not touched yet.
 */
function setTab(tab, { push = true, keepPage = false, gesture = false } = {}) {
  state.tab = TABS.includes(tab) ? tab : "all";
  if (gesture && state.tab === "nearme") locateForNearMe();
  for (const button of document.querySelectorAll("[data-tab]")) {
    const active = button.dataset.tab === state.tab;
    button.classList.toggle("on", active);
    button.setAttribute("aria-selected", String(active));
  }
  // Hide whichever control this section already decides for you. Near me
  // ignores the country entirely, so leaving the filter on screen would invite
  // a narrowing that the section then refuses to apply.
  $("f-country").hidden = state.tab === "nearme";
  const types = state.tab === "types";
  $("types").hidden = !types;
  $("controls").hidden = types;
  $("resultbar").hidden = types;
  $("results").hidden = types;
  $("pager-bottom").hidden = types;
  if (push) update({ keepPage });
}

// ------------------------------------------------------------------- setup

function option(value, text, title) {
  const node = el("option", null, text);
  node.value = value;
  if (title) node.title = title;
  return node;
}

const byCount = (rows) => [...rows].sort((a, b) => b.n - a.n);

function fillSimple(select, rows, labels) {
  select.replaceChildren(option("", "Any"));
  for (const row of rows) {
    select.append(option(row.value,
      `${labels ? label(labels, row.value) : row.value} (${row.n})`,
      labels ? blurb(labels, row.value) : ""));
  }
}

/* The film select is the one control people misread: its per-model buckets are
 * a partition of the 70 mm venues, so a single model always shows a much
 * smaller number than the "70 mm film only" toggle.  Leading with an explicit
 * "any" entry - which simply hands the filter to that toggle - makes the
 * relationship obvious instead of surprising. */
function fillFilmSelect(select, rows, total70) {
  select.replaceChildren(option("", "Any"));
  select.append(option(ANY_FILM, `Any 15/70 mm film (${total70})`,
    "Every theatre still running real film — the same set as the 70 mm toggle."));
  const group = el("optgroup");
  group.label = "Specific projector model";
  for (const row of byCount(rows)) {
    group.append(option(row.value, `${label(FILM_LABELS, row.value)} (${row.n})`,
      blurb(FILM_LABELS, row.value)));
  }
  select.append(group);
}

function fillCities() {
  state.cities = cityIndex(state.venues);
}

function fillControls(facets) {
  fillSimple(CONTROLS.country, facets.countries);
  fillSimple(CONTROLS.projector, byCount(facets.projectors), DIGITAL_LABELS);
  fillFilmSelect(CONTROLS.film, facets.films, facets.film70);

  const ars = Object.fromEntries(facets.ars.map((a) => [a.value, a.n]));
  CONTROLS.ar.replaceChildren(
    option("", "Any"),
    option("1.43", `1.43:1 — full height (${ars["1.43"]})`,
      "The tallest IMAX frame. Includes dome venues; add the Dome filter to separate them."),
    option("1.90", `1.90:1 (${ars["1.90"]})`,
      "The widescreen IMAX frame used by most commercial auditoriums."),
  );

}

/* The promoted country filter is also the "which country am I asking about"
 * control, so a deliberate choice is worth remembering the way the old picker's
 * was. Clearing it back to Anywhere is not a country, so it does not overwrite
 * a stored one - a visitor widening the list has not moved house. */
function setCountry(country, { remember = true } = {}) {
  CONTROLS.country.value = country;
  // Touching the control at all makes it a choice, including choosing
  // Anywhere - which is why a widened list keeps its `country=any` in the URL
  // instead of springing back to your own country on the next load.
  state.countryAuto = false;
  if (country) {
    state.country = country;
    if (remember) storeCountry(country);
  }
  update();
}

// ------------------------------------------------------- precise location

/* Asking for a position is never something the page does on its own.
 *
 * Every path into this function starts at a click: the "Nearest first" sort, or
 * the button in the Near me section. The one exception is a reload where
 * permission was already granted, which raises no prompt at all - see start().
 *
 * A refusal is final for the visit. The sort reverts to location, the reason is
 * shown once, and nothing asks again; browsers remember a denial anyway, so a
 * retry would be a prompt the visitor never sees and a spinner that never ends.
 */
/**
 * Entering the Near me section. Asks once, and only when asking can lead
 * anywhere: not while a request is in flight, not when we already have a fix,
 * and not after a refusal - browsers remember a denial, so re-asking would show
 * the visitor nothing and spin forever.
 */
function locateForNearMe() {
  if (!canLocate() || state.origin) return;
  if (state.geoStatus === "locating" || GEO_FINAL.includes(state.geoStatus)) return;
  requestPosition();
}

async function requestPosition() {
  if (state.geoStatus === "locating") return false;
  setGeoStatus("locating");
  try {
    const fix = await locate();
    state.origin = { lat: fix.lat, lon: fix.lon };
    state.geoAccuracy = fix.accuracy || 0;
    setGeoStatus("located");
    CONTROLS.sort.value = "distance";
    update();
    return true;
  } catch (err) {
    // A named city outranks no answer at all: if the visitor has told us where
    // they are, a failed measurement falls back to that rather than to an
    // unranked list.
    if (state.originCity && setManualOrigin(state.originCity)) return false;
    state.origin = null;
    setGeoStatus(err.code || "unavailable");
    // Leave the control saying what the list actually does.
    if (CONTROLS.sort.value === "distance") CONTROLS.sort.value = "location";
    update();
    return false;
  }
}

/**
 * Handle a link that arrives already wanting a position - either `?sort=distance`
 * or `?tab=nearme`.
 *
 * This is the one place a position could be resolved without a click, so it is
 * also the one place that has to be careful. Permission is checked before
 * anything is requested: if it was granted on a previous visit the fix costs no
 * prompt, and the link opens as its sender meant it to. Otherwise the page must
 * not greet a stranger with a dialog it has not earned - Near me says what it
 * is waiting for, and a distance sort quietly becomes location with the control
 * reset to admit it.
 */
async function restorePosition() {
  const wanted = CONTROLS.sort.value === "distance" || state.tab === "nearme";
  if (!wanted) return;
  if (state.origin && state.geoStatus !== "manual") return;

  if (!canLocate() || await permissionState() !== "granted") {
    if (!state.origin && CONTROLS.sort.value === "distance") {
      CONTROLS.sort.value = "location";
      update();
    }
    return;
  }

  // A remembered city is already ranking the list, so the upgrade to a
  // measured fix happens without touching geoStatus: flipping the banner to
  // "Finding you\u2026" and back for an attempt the visitor never asked to
  // watch would be flicker in the one place the page had just settled.
  if (state.origin) {
    try {
      const got = await locate({ patience: 5000 });
      state.origin = { lat: got.lat, lon: got.lon };
      state.geoAccuracy = got.accuracy || 0;
      setGeoStatus("located");
      CONTROLS.sort.value = "distance";
      update();
    } catch {
      /* the named city stands */
    }
    return;
  }
  await requestPosition();
}

function setGeoStatus(status) {
  state.geoStatus = status;
  renderGeoStatus();
}

/* The reason the list is in the order it is, whenever a location was involved.
 * The Near me section carries its own headline; this is the line under the
 * count, which is where someone looking at the rows themselves will be. */
function renderGeoStatus() {
  const box = $("geostatus");
  let text = state.tab === "nearme" || state.origin
    ? explainPosition(state.geoStatus, state.geoAccuracy)
    : "";
  if (state.geoStatus === "manual" && state.origin
      && (state.tab === "nearme" || CONTROLS.sort.value === "distance")) {
    text = `Distances measured from ${state.originCity} \u2014 a city you named, `
      + "not a detected position. Nothing was stored but the name.";
  }
  box.textContent = text;
  box.hidden = !text;
}

function restoreFromUrl() {
  const params = new URLSearchParams(location.search);
  CONTROLS.q.value = params.get("q") || "";
  for (const key of TOGGLES) setOn(CONTROLS[key], params.get(key) === "1");
  for (const key of ["projector", "film", "ar", "sort"]) {
    if (params.get(key)) CONTROLS[key].value = params.get(key);
  }

  // No `country` at all means "wherever the visitor is", and start() fills it
  // in. `any` is the explicit opposite, and the only way to say "everywhere" in
  // a link that survives a reload.
  const country = params.get("country");
  state.countryAuto = !country;
  if (country) CONTROLS.country.value = country === "any" ? "" : country;
  state.region = params.get("region") || "";
  state.page = Math.max(1, parseInt(params.get("page"), 10) || 1);
  const tab = params.get("tab") || "all";
  return TAB_ALIASES[tab] || tab;
}

function wire() {
  CONTROLS.q.addEventListener("input", debounce(update, 160));
  for (const key of ["projector", "ar"]) {
    CONTROLS[key].addEventListener("change", update);
  }

  // The whole pill is the control, so the click lands on the button itself
  // rather than on a checkbox inside it.
  for (const key of TOGGLES) {
    CONTROLS[key].addEventListener("click", () => {
      setOn(CONTROLS[key], !isOn(CONTROLS[key]));
      update();
    });
  }

  // Choosing a country here is also choosing which country My Country reports
  // on, so it goes through setCountry() and gets remembered.
  CONTROLS.country.addEventListener("change", () => setCountry(CONTROLS.country.value));

  // Choosing "Nearest first" is itself the consent gesture - it is a click, and
  // it says plainly what the position is wanted for - so the prompt is raised
  // here rather than behind a separate button.
  CONTROLS.sort.addEventListener("change", () => {
    if (CONTROLS.sort.value === "distance" && !state.origin) {
      requestPosition();
      return;
    }
    update();
  });

  // Picking "Any 15/70 mm film" hands the filter to the toggle that owns it,
  // so the select and the quick filter can never end up disagreeing.
  CONTROLS.film.addEventListener("change", () => {
    if (CONTROLS.film.value === ANY_FILM) {
      CONTROLS.film.value = "";
      setOn(CONTROLS.film70, true);
    }
    update();
  });

  for (const button of document.querySelectorAll("[data-tab]")) {
    button.addEventListener("click", () => setTab(button.dataset.tab, { gesture: true }));
  }

  $("reset").addEventListener("click", () => { resetFilters(); update(); });

  /* The masthead is the way back to the start, which is what a title in that
   * position promises. It lands on the same state a first visit does - every
   * filter off, All venues, page one, and the country back to the guess rather
   * than to Anywhere, since that guess is part of the front page rather than
   * something the visitor applied.
   *
   * Modified clicks are left alone so the href does its job: a cmd-click that
   * silently reset the page in the current tab instead of opening a new one
   * would be the link lying about being a link. */
  $("home").addEventListener("click", (e) => {
    if (e.metaKey || e.ctrlKey || e.shiftKey || e.altKey || e.button !== 0) return;
    e.preventDefault();
    resetFilters();
    // Back to the guess, and back to being a guess: countryAuto is what keeps
    // it out of the URL, so the address bar ends up at "/" like a first visit
    // rather than at "/?country=India".
    CONTROLS.country.value = state.country;
    state.countryAuto = true;
    setTab("all");
    window.scrollTo({ top: 0 });
  });

  document.addEventListener("keydown", (e) => {
    if (e.key === "/" && document.activeElement !== CONTROLS.q) {
      e.preventDefault();
      CONTROLS.q.focus();
      CONTROLS.q.select();
    }
  });
}

/* Three audiences, three destinations, named in the order most people need
 * them. Venue facts belong upstream on the wiki, where a fix reaches everyone
 * rather than only this site. A broken page or link is ours, and reaches us by
 * email so that no account is needed. Developers who would rather file an issue
 * get that route too, once the repository exists.
 *
 * The report used to sit on every venue card, which meant 476 copies of a link
 * almost nobody clicks and which the reporter did not need pointed out. One
 * link here does the job - but since the reader is no longer standing on a
 * particular theatre when they click it, the email has to ask which one. */
function reportEmailUrl(email) {
  const body = [
    "Which theatre is this about?",
    "(name and city - or say 'the site itself' for a page problem)",
    "",
    "",
    "What is wrong?",
    "(a broken link, wrong projector, wrong screen size, wrong location...)",
    "",
    "",
    "-- ",
    `Reported from ${location.href}`,
  ].join("\n");

  return `mailto:${email}`
    + `?subject=${encodeURIComponent("FindMaxScreen: a problem")}`
    + `&body=${encodeURIComponent(body)}`;
}

function renderReportNote(links) {
  const note = $("reportnote");
  if (!note || !(links.wiki || links.email || links.repo)) return;
  note.replaceChildren();
  note.hidden = false;

  const add = (text) => note.append(document.createTextNode(text));
  const link = (href, text) => {
    const a = el("a", null, text);
    a.href = href;
    if (!href.startsWith("mailto:")) {
      a.target = "_blank";
      a.rel = "noreferrer";
    }
    note.append(a);
  };

  add("Found a mistake? Venue details — projector, screen, location — come from the ");
  link(links.wiki, "IMAX Wiki");
  add(", and correcting them there fixes them for everyone and flows back here "
      + "on the next sync.");

  if (links.email) {
    add(" For a broken link, wrong data or anything else amiss, ");
    link(reportEmailUrl(links.email), "report it by email");
    add(" — tell us which theatre and what is wrong.");
  }
  if (links.repo) {
    add(" Developers can ");
    link(`${links.repo}/issues/new?`
         + new URLSearchParams({ title: "Site: ", labels: "site" }),
         "open an issue");
    add(" instead.");
  }
}

// ------------------------------------------------------------------ themes

const THEMES = ["auto", "light", "dark"];
const THEME_ICON = { auto: "auto", light: "sun", dark: "moon" };

function applyTheme(theme) {
  if (theme === "auto") delete document.documentElement.dataset.theme;
  else document.documentElement.dataset.theme = theme;
  const button = $("theme");
  button.querySelector("use").setAttribute("href", `#i-${THEME_ICON[theme]}`);
  $("thememode").textContent = theme[0].toUpperCase() + theme.slice(1);
  button.title = `Theme: ${theme}. Click for ${
    THEMES[(THEMES.indexOf(theme) + 1) % THEMES.length]}.`;
}

function wireTheme() {
  let theme = localStorage.getItem("theme");
  if (!THEMES.includes(theme)) theme = "auto";
  applyTheme(theme);
  $("theme").addEventListener("click", () => {
    theme = THEMES[(THEMES.indexOf(theme) + 1) % THEMES.length];
    localStorage.setItem("theme", theme);
    applyTheme(theme);
  });
}

// ------------------------------------------------------------------- start

async function start() {
  wireTheme();

  let data;
  try {
    const resp = await fetch("data/venues.json", { cache: "no-cache" });
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    data = await resp.json();
  } catch (err) {
    $("tagline").textContent =
      `Could not load the venue data (${err.message}). Run ./export.py.`;
    return;
  }

  state.data = data;
  state.venues = index(data.venues);

  const s = data.stats;
  $("tagline").textContent =
    `${fmt.format(s.venues)} IMAX theatres across ${fmt.format(s.countries)} countries. `
    + `Only ${fmt.format(s.film70)} still run 15/70 mm film — those are the ones worth travelling for.`;

  renderReportNote(data.links || {});

  const rev = data.revision;
  $("asof").textContent = rev
    ? `Wiki revision ${rev.revid}, edited ${new Date(rev.wiki_timestamp).toLocaleDateString()}`
      + ` · data built ${new Date(data.generated_at).toLocaleDateString()}`
    : "No revision recorded";

  fillControls(data.facets);
  fillCities();

  // A city named on an earlier visit is a standing answer to "where are you?",
  // so it comes back without being asked for - permission never enters into it.
  const rememberedCity = readStoredCity();
  if (rememberedCity && state.cities.has(rememberedCity)) {
    const coords = state.cities.get(rememberedCity);
    state.origin = { lat: coords.lat, lon: coords.lon };
    state.originCity = rememberedCity;
    state.geoStatus = "manual";
  }

  state.detection = detectCountry(data.geo || {});
  state.country = state.detection.country || "";

  // An order the browser cannot produce should not be on the menu at all.
  if (!canLocate()) $("sort-distance")?.remove();

  const tab = restoreFromUrl();

  // Applied after the URL has been read, because the URL is what decides
  // whether it applies at all: a link naming a country wins, `country=any`
  // means the sender deliberately widened it, and only a link that says
  // nothing about countries gets the guess.
  //
  // A guess with no venues - Norway, say - leaves the select on Anywhere, since
  // the facets only list countries that have some. shownCountry() still falls
  // back to it, so My Country can report "nothing listed there" honestly.
  if (state.countryAuto) CONTROLS.country.value = state.country;

  wire();
  // A shared link can arrive with filters already applied. Landing on a shut
  // panel would make the narrowed result set look like the whole dataset, so
  // open it when - and only when - the URL actually set something.
  if (panelFiltersActive()) $("filterpanel").open = true;
  // keepPage so a shared ?page=3 link lands where it says it will.
  setTab(tab, { keepPage: true });

  // Last, and deliberately after the first paint: resolving a position may
  // await the Permissions API, and the list must not wait on that to appear.
  await restorePosition();
}

start();
