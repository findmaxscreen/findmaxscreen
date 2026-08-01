#!/usr/bin/env python3
"""Assertions about the data that is about to be published.

The other suites test code. This one tests the *bundle* — the actual JSON that
will be served — and it is the last gate before a deploy that nobody is watching.

Everything here is local and offline: it reads `web/data/venues.json` and the
committed database, so it costs nothing and can run on every push. It exists
because a parser can fail halfway and still produce a plausible-looking file:
the counts stay in the right shape while a whole class of venue quietly
disappears. Shape is not enough; these check the numbers agree with each other.

    python3 test_bundle.py
"""

import json
import re
import sqlite3
import unicodedata
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
DATA = HERE / "web" / "data" / "venues.json"
DB = HERE / "theatres.sqlite3"

# How far the venue count may move between deploys before a human should look.
# The wiki gains or loses a handful of venues a month; a 15% swing overnight is
# a parser fault or vandalism, not news.
COUNT_TOLERANCE = 0.15

# Words too common to prove a geocode matched the right place.
STOPWORDS = {
    "the", "and", "imax", "cinema", "cinemas", "cinemark", "cineplex",
    "theatre", "theater", "theatres", "theaters", "mall", "centre", "center",
    "city", "cine", "multiplex", "megaplex",
}


def fold(text: str) -> str:
    """Lower-case and strip accents, so 'Aubiere' matches 'Aubière'."""
    decomposed = unicodedata.normalize("NFKD", text or "")
    return "".join(c for c in decomposed if not unicodedata.combining(c)).lower()


def tokens(text: str) -> set[str]:
    return {t for t in re.findall(r"[^\W_]+", fold(text))
            if len(t) > 2 and t not in STOPWORDS}


def related(a: set[str], b: set[str]) -> bool:
    """Do two name token-sets share a word, allowing for inflection?

    An exact set intersection is too strict for place names: the Kraków venue
    is "Cinema City Zakopianka" and sits on ulica Zakopiańska. Those are the
    same place and obviously related, but no token matches exactly. Treating a
    shared five-character prefix as a match catches that whole family of
    Slavic/Romance declensions without letting genuinely different names pass.
    """
    if a & b:
        return True
    return any(x[:5] == y[:5] for x in a for y in b
               if len(x) >= 5 and len(y) >= 5)


def is_latin(text: str) -> bool:
    """False when the string is largely CJK, where token overlap proves nothing."""
    return all(ord(c) < 0x2E80 for c in text if c.isalpha())


class BundleCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not DATA.is_file():
            raise unittest.SkipTest(f"{DATA} missing; run ./export.py first")
        cls.payload = json.loads(DATA.read_text())
        cls.venues = cls.payload["venues"]
        cls.live = [v for v in cls.venues if v["removed_at"] is None]


class TestCounts(BundleCase):
    def test_the_bundle_is_not_empty(self):
        self.assertGreater(len(self.live), 0, "no live venues in the bundle")

    def test_venue_count_has_not_swung_wildly(self):
        """Compare against the committed database, which is the last good state."""
        if not DB.is_file():
            self.skipTest("no database to compare against")
        conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
        try:
            previous = conn.execute(
                "SELECT count(*) FROM venues WHERE removed_at IS NULL").fetchone()[0]
        finally:
            conn.close()
        if not previous:
            self.skipTest("database has no venues yet")
        drift = abs(len(self.live) - previous) / previous
        self.assertLessEqual(
            drift, COUNT_TOLERANCE,
            f"live venue count moved {drift:.0%} ({previous} -> {len(self.live)}); "
            f"check the wiki before publishing")

    def test_film_venues_still_exist(self):
        """The whole point of the site. Zero means the film classifier broke."""
        self.assertGreater(self.payload["stats"]["film70"], 0)

    def test_stats_agree_with_the_venue_list(self):
        stats = self.payload["stats"]
        self.assertEqual(stats["venues"], len(self.live))
        self.assertEqual(stats["film70"], sum(v["has_70mm"] for v in self.live))
        self.assertEqual(stats["dome"], sum(v["is_dome"] for v in self.live))
        self.assertEqual(stats["removed"], len(self.venues) - len(self.live))


class TestFacets(BundleCase):
    def test_every_facet_count_matches_the_venues_it_labels(self):
        facets = self.payload["facets"]
        for facet, field in (("countries", "country"), ("regions", "region"),
                             ("projectors", "projector_family"),
                             ("films", "film_family")):
            for row in facets[facet]:
                with self.subTest(facet=facet, value=row["value"]):
                    actual = sum(1 for v in self.live if v[field] == row["value"])
                    self.assertEqual(row["n"], actual)

    def test_film_buckets_partition_the_70mm_venues(self):
        """The UI leans on this: per-model counts must sum to the toggle's count."""
        films = self.payload["facets"]["films"]
        self.assertEqual(sum(f["n"] for f in films),
                         self.payload["facets"]["film70"])

    def test_aspect_ratio_facets_match_the_flags(self):
        ars = {a["value"]: a["n"] for a in self.payload["facets"]["ars"]}
        self.assertEqual(ars["1.43"], sum(v["is_1_43"] for v in self.live))


class TestVenueFields(BundleCase):
    def test_every_venue_can_have_a_map_link_built_for_it(self):
        """maps_url is not shipped — it was 55 KB of a deterministic string —
        so the page rebuilds it from these fields. They must all be present."""
        unbuildable = [v["name"] for v in self.live
                       if not [p for p in (v["name"], v["city"], v["country"]) if p]]
        self.assertEqual(unbuildable, [])

    def test_every_venue_has_a_name_and_a_place(self):
        for v in self.live:
            with self.subTest(venue=v["name"]):
                self.assertTrue(v["name"])
                self.assertTrue(v["country"])
                self.assertTrue(v["city"])

    def test_no_raw_wikitext_or_provenance_leaked(self):
        exported = set(self.venues[0])
        for private in ("venue_key", "digital_raw", "film_raw", "dimensions_raw",
                        "first_seen_revid", "geo_matched", "geocoded_at"):
            self.assertNotIn(private, exported)


class TestGeocoding(BundleCase):
    def test_coverage_has_not_collapsed(self):
        located = [v for v in self.live if v["lat"] is not None]
        self.assertGreater(
            len(located) / len(self.live), 0.80,
            "geocode coverage dropped below 80%; did a run get interrupted?")

    def test_coordinates_are_on_earth(self):
        for v in self.live:
            if v["lat"] is not None:
                with self.subTest(venue=v["name"]):
                    self.assertTrue(-90 <= v["lat"] <= 90)
                    self.assertTrue(-180 <= v["lon"] <= 180)

    def test_precision_and_coordinates_travel_together(self):
        for v in self.live:
            with self.subTest(venue=v["name"]):
                if v["geo_precision"] in ("venue", "city"):
                    self.assertIsNotNone(v["lat"])
                else:
                    self.assertIsNone(v["lat"])

    def test_exact_fixes_match_the_place_they_claim(self):
        """Audit every precise coordinate against the OSM feature it matched.

        Only possible because geocode.py stores `geo_matched`; before that this
        cost one reverse-geocode call per venue and could only be sampled.
        Diacritics are folded (Aubiere/Aubière) and CJK names exempted, since
        token overlap says nothing across scripts.
        """
        if not DB.is_file():
            self.skipTest("no database to audit against")
        conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                "SELECT name, geo_matched FROM venues"
                " WHERE geo_precision = 'venue' AND removed_at IS NULL").fetchall()
        finally:
            conn.close()

        checkable = [r for r in rows if is_latin(r["geo_matched"])]
        mismatched = [f"{r['name']} -> {r['geo_matched']}" for r in checkable
                      if not related(tokens(r["name"]), tokens(r["geo_matched"]))]

        # A rate, not zero. This is a name heuristic over 200-odd places in a
        # dozen languages, so an occasional legitimate miss is expected — a gate
        # that cries wolf on a correct match is one people learn to ignore. The
        # thing worth failing a deploy for is a *cluster*, which is what a
        # broken name-matching change would produce.
        rate = len(mismatched) / max(len(checkable), 1)
        self.assertLessEqual(
            rate, 0.05,
            f"{len(mismatched)} of {len(checkable)} exact fixes matched a place "
            f"sharing no word with the venue — probably the wrong building:\n  "
            + "\n  ".join(mismatched[:10]))


class TestReporting(BundleCase):
    def test_the_links_block_ships(self):
        self.assertIn("links", self.payload)

    def test_the_wiki_is_always_named(self):
        """Data corrections belong upstream, so that link is never optional."""
        self.assertTrue(self.payload["links"]["wiki"].startswith("https://"))

    def test_no_email_address_is_published(self):
        """The mailto was retired because a published address is harvested
        within days, and because email cannot require the venue or the URL that
        make a report actionable. This asserts it stays retired: the wiki is
        the no-account route now, and the issue form is the structured one.

        Checked against the whole payload rather than links["email"], because
        the way this comes back is somebody adding an address to a data note or
        a new links entry, not restoring the key that was deleted."""
        blob = json.dumps(self.payload)
        found = re.findall(r"[\w.+-]+@[\w-]+\.[\w.]+", blob)
        self.assertEqual(found, [], f"an email address reached the bundle: {found}")

    def test_no_email_address_appears_in_published_html(self):
        """The companion to the test above, guarding the other half.

        This is the mistake that actually happened: the privacy page spelled an
        address out in an <a href="mailto:">, which is the single most
        harvestable form there is, while every other page was careful to build
        its links at runtime. 158 unit tests passed through it, because none of
        them read the HTML. This one does."""
        import export

        offenders = {}
        for name in export.PUBLIC_FILES:
            if not name.endswith(".html"):
                continue
            found = re.findall(r"[\w.+-]+@[\w-]+\.[\w.]+",
                               (HERE / "web" / name).read_text())
            if found:
                offenders[name] = found
        self.assertEqual(offenders, {},
                         "an email address is sitting in published HTML where "
                         f"a harvester will find it: {offenders}")

    def test_the_issue_form_the_site_links_to_exists_and_demands_the_basics(self):
        """app.js sends reporters to ?template=site.yml. If that file is renamed
        the link does not 404 - GitHub quietly falls back to the chooser - so
        nothing would ever tell us the prefill had stopped working.

        The required fields are the point of the form, and of retiring the
        mailto: an email could ask for the URL and be ignored. This asserts the
        form still cannot be submitted without it.

        Scanned as text rather than parsed. PyYAML is not in the standard
        library and nothing else here needs it; adding a pip install to the
        workflow to check four booleans is a worse trade than a block scan."""
        form = HERE / ".github" / "ISSUE_TEMPLATE" / "site.yml"
        self.assertTrue(form.is_file(), "app.js links at site.yml; it is missing")

        # One block per "- type:" entry, each carrying its own id and its own
        # required flag - which is all this needs to know.
        blocks = re.split(r"^  - type:", form.read_text(), flags=re.M)[1:]
        required = set()
        for block in blocks:
            found = re.search(r"^    id: (\S+)", block, flags=re.M)
            if found and re.search(r"required: true", block):
                required.add(found.group(1))

        for field in ("what", "url", "browser", "known"):
            self.assertIn(field, required,
                          f"{field!r} must stay required, or reports arrive unactionable")

    def test_blank_issues_stay_disabled(self):
        """The back door. With blank issues on, every required field above is
        optional again by simply not using the template."""
        config = (HERE / ".github" / "ISSUE_TEMPLATE" / "config.yml").read_text()
        # (?m) because the setting sits below the comment block, and assertRegex
        # does a plain search where ^ would otherwise only match the file start.
        self.assertRegex(config, r"(?m)^blank_issues_enabled:\s*false\s*$",
                         "blank issues bypass every required field on the form")

    def test_repo_is_either_a_url_or_deliberately_empty(self):
        """An unset repo hides the report links rather than shipping dead ones."""
        repo = self.payload["links"]["repo"]
        self.assertTrue(repo == "" or repo.startswith("https://"),
                        f"repo must be a URL or empty, got {repo!r}")

    def test_a_configured_repo_has_no_trailing_slash(self):
        """reportUrl() appends /issues/new, so a trailing slash would double up."""
        repo = self.payload["links"]["repo"]
        if repo:
            self.assertFalse(repo.endswith("/"))


class TestGeoBlock(BundleCase):
    def test_the_timezone_map_is_present_and_populated(self):
        geo = self.payload["geo"]
        self.assertGreater(len(geo["timezones"]), 100)
        self.assertEqual(geo["timezones"].get("Asia/Calcutta"), "IN")

    def test_every_country_in_the_data_can_be_recognised(self):
        """A visitor's country is matched by name; an unmapped one is invisible."""
        named = set(self.payload["geo"]["countries"].values())
        present = {v["country"] for v in self.live}
        self.assertEqual(sorted(present - named), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
