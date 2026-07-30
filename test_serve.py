#!/usr/bin/env python3
"""Tests for the read-only query layer in serve.py.

These exercise VenueStore directly rather than over HTTP: the interesting logic
is in how query parameters become SQL, not in the socket.  Several of these are
regression guards for filters whose counts silently disagreed with the data.

    python3 test_serve.py
"""

import tempfile
import unittest
from pathlib import Path

import serve
import sync

FIXTURES = [
    # name,               city,        country,  screen_ar,      film,                  digital,            dims,          commercial
    ("Flat 70",           "Vancouver", "Canada", "1.43:1",       "IMAX GT3D 15/70 mm",  "IMAX GT Laser",    "27.6 m × 19.3 m\n533 m²", "Yes"),
    ("Dome 70",           "Bruxelles", "Belgium", "Dome 1.43:1",  "IMAX SR Dome 15/70 mm", "IMAX Dome Laser", "23 m",       "No"),
    ("Bare 70",           "Irvine",    "United States", "1.43:1", "IMAX 15/70 mm",       "IMAX CoLa",        "",            "Yes"),
    ("Digital only",      "Tokyo",     "Japan",  "1.90:1",        "",                    "IMAX CoLa",        "20 m × 10 m", "Yes"),
    ("Typo AR",           "Thu Duc",   "Vietnam", "1:90:1",       "",                    "IMAX Laser XT",    "",            "Yes"),
    ("Sao Paulo Cinema",  "São Paulo", "Brazil", "1.90:1",        "",                    "IMAX CoLa",        "",            "Yes"),
    # Dropped from revision 2 below, so the soft-delete paths have a subject.
    ("Closed Cinema",     "Nowhere",   "Atlantis", "1.90:1",      "",                    "IMAX CoLa",        "",            "No"),
]


def build(fixtures=FIXTURES):
    return [
        sync.build_record("Europe", {
            "country": country, "city": city, "name": name,
            "screen_ar": ar, "film_raw": film, "digital_raw": digital,
            "dimensions_raw": dims, "commercial_films": commercial,
        })
        for name, city, country, ar, film, digital, dims, commercial in fixtures
    ]


class StoreCase(unittest.TestCase):
    """A store over a throwaway database holding the fixtures above."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        db = Path(cls.tmp.name) / "test.sqlite3"
        conn = sync.connect(db)
        sync.apply_revision(conn, build(), 1, "2026-01-01T00:00:00Z",
                            "2026-01-01T00:00:00+00:00", "sha", "snap.wiki")
        # One venue vanishes upstream, so include_removed has something to find.
        sync.apply_revision(conn, build(FIXTURES[:-1]), 2, "2026-02-01T00:00:00Z",
                            "2026-02-01T00:00:00+00:00", "sha", "snap.wiki")
        conn.close()
        cls.store = serve.VenueStore(db)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def query(self, **params):
        wrapped = {k: [str(v)] for k, v in params.items()}
        return self.store.venues(wrapped)

    def names(self, **params):
        return [v["name"] for v in self.query(**params)["venues"]]


class TestFtsMatch(unittest.TestCase):
    def test_splits_into_prefix_terms(self):
        self.assertEqual(serve.fts_match("van cou"), '"van"* AND "cou"*')

    def test_single_term(self):
        self.assertEqual(serve.fts_match("tokyo"), '"tokyo"*')

    def test_empty_and_punctuation_only_yield_no_match(self):
        for text in ("", "   ", "!!!", "-", '"'):
            with self.subTest(text=text):
                self.assertIsNone(serve.fts_match(text))

    def test_quotes_cannot_escape_the_query(self):
        # The tokenizer keeps only word characters, so a crafted string cannot
        # break out of the quoted FTS term.
        self.assertEqual(serve.fts_match('a" OR b'), '"a"* AND "OR"* AND "b"*')

    def test_accented_input_is_tokenized(self):
        self.assertEqual(serve.fts_match("São Paulo"), '"São"* AND "Paulo"*')


class TestSearch(StoreCase):
    def test_prefix_search_finds_a_city(self):
        self.assertEqual(self.names(q="vancou"), ["Flat 70"])

    def test_search_is_diacritic_insensitive(self):
        # "Sao Paulo" must find "São Paulo"; that is why the FTS table sets
        # remove_diacritics 2.
        self.assertEqual(self.names(q="sao paulo"), ["Sao Paulo Cinema"])

    def test_search_matches_theatre_name(self):
        self.assertEqual(self.names(q="dome"), ["Dome 70"])

    def test_nonsense_search_returns_nothing(self):
        self.assertEqual(self.query(q="zzzznotathing")["total"], 0)


class TestFilters(StoreCase):
    def test_film70_returns_only_15_70_venues(self):
        self.assertEqual(sorted(self.names(film70=1)),
                         ["Bare 70", "Dome 70", "Flat 70"])

    def test_dome_filter(self):
        self.assertEqual(self.names(dome=1), ["Dome 70"])

    def test_commercial_filter_excludes_no_and_unknown(self):
        self.assertNotIn("Dome 70", self.names(commercial=1))

    def test_country_filter(self):
        self.assertEqual(self.names(country="Canada"), ["Flat 70"])

    def test_filters_combine(self):
        self.assertEqual(self.names(film70=1, dome=1), ["Dome 70"])

    def test_ar_1_43_includes_domes(self):
        # Regression guard: "Dome 1.43:1" is still a 1.43 screen, and excluding
        # domes here made the filter under-report by 28 venues against the wiki.
        self.assertEqual(sorted(self.names(ar="1.43")),
                         ["Bare 70", "Dome 70", "Flat 70"])

    def test_ar_1_43_plus_dome_toggle_narrows_to_domes(self):
        self.assertEqual(self.names(ar="1.43", dome=1), ["Dome 70"])

    def test_ar_1_90_includes_the_repaired_typo(self):
        # "1:90:1" is normalized at parse time, so the venue is reachable.
        self.assertIn("Typo AR", self.names(ar="1.90"))


class TestRemoved(StoreCase):
    def test_removed_venues_are_hidden_by_default(self):
        self.assertNotIn("Closed Cinema", self.names())

    def test_include_removed_brings_them_back(self):
        self.assertIn("Closed Cinema", self.names(include_removed=1))

    def test_facet_counts_ignore_removed_venues(self):
        countries = {c["value"]: c["n"] for c in self.store.facets()["countries"]}
        self.assertNotIn("Atlantis", countries)

    def test_meta_reports_removed_separately(self):
        stats = self.store.meta()["stats"]
        self.assertEqual(stats["venues"], 6)
        self.assertEqual(stats["removed"], 1)
        self.assertEqual(stats["film70"], 3)


class TestSorting(StoreCase):
    def test_size_sort_is_biggest_first(self):
        areas = [v["screen_area_m2"] for v in self.query(sort="size")["venues"]
                 if v["screen_area_m2"] is not None]
        self.assertEqual(areas, sorted(areas, reverse=True))

    def test_size_sort_puts_unknown_areas_last(self):
        venues = self.query(sort="size")["venues"]
        first_null = next(i for i, v in enumerate(venues)
                          if v["screen_area_m2"] is None)
        self.assertTrue(all(v["screen_area_m2"] is None
                            for v in venues[first_null:]))

    def test_name_sort(self):
        names = self.names(sort="name")
        self.assertEqual(names, sorted(names, key=str.lower))

    def test_unknown_sort_falls_back_to_location(self):
        self.assertEqual(self.names(sort="; DROP TABLE venues"),
                         self.names(sort="location"))


class TestLimits(StoreCase):
    def test_limit_truncates_but_total_is_the_full_count(self):
        result = self.query(limit=2)
        self.assertEqual(result["shown"], 2)
        self.assertEqual(result["total"], 6)

    def test_limit_is_capped(self):
        self.assertLessEqual(self.query(limit=999999)["shown"], serve.MAX_LIMIT)

    def test_garbage_limit_does_not_raise(self):
        self.assertEqual(self.query(limit="lots")["total"], 6)


class TestFacets(StoreCase):
    def test_facets_expose_each_dimension(self):
        facets = self.store.facets()
        self.assertEqual(sorted(facets),
                         ["ars", "countries", "film70", "films", "projectors", "regions"])

    def test_ar_facet_counts_match_what_the_filter_returns(self):
        # The dropdown label and the result count come from different queries;
        # if they ever drift apart the UI starts contradicting itself.
        ars = {a["value"]: a["n"] for a in self.store.facets()["ars"]}
        for value, count in ars.items():
            with self.subTest(ar=value):
                self.assertEqual(self.query(ar=value)["total"], count)

    def test_film70_facet_matches_the_toggle(self):
        self.assertEqual(self.store.facets()["film70"],
                         self.query(film70=1)["total"])

    def test_film_family_buckets_partition_the_70mm_venues(self):
        # The UI leans on this: the per-model counts must add up to the number
        # the "70 mm film only" toggle reports, or the two disagree on screen.
        films = {f["value"]: f["n"] for f in self.store.facets()["films"]}
        self.assertEqual(sum(films.values()), self.store.meta()["stats"]["film70"])

    def test_blank_values_are_not_offered_as_choices(self):
        for name, rows in self.store.facets().items():
            if isinstance(rows, list):
                with self.subTest(facet=name):
                    self.assertNotIn("", [r["value"] for r in rows])


if __name__ == "__main__":
    unittest.main(verbosity=2)
