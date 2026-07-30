#!/usr/bin/env python3
"""Parser tests for sync.py, run against fixtures rather than the live wiki.

The fixtures below are trimmed from the real page and keep its awkward parts:
nested rowspans (Brazil -> São Paulo), the two different column layouts, wiki
links with anchors and pipes, dimensions written both spaced and unspaced, and
every observed spelling of the projector names.

    python3 test_sync.py
"""

import unittest

import sync

# Europe-style layout: 9 columns, no State/Province.
EUROPE = """== Europe ==
{| class="sortable fandom-table"
!Country
!City
!Location Name
!Screen Aspect Ratio (AR)
!Digital Projector
!Maximum AR for digital projection
!Film Projector
!Screen dimensions
!Commercial films shown?
|-
| rowspan="2" |Austria
|[[Graz]]
|CineplexX Graz & IMAX
|1.90:1
|IMAX CoLa
|1.90:1
|
|17.4 m × 9.2 m
57.1 ft × 30.2 ft
|Yes
|-
|[[Vienna]]
|CineplexX Apollo Vienna & IMAX
|1.90:1
|IMAX CoLA
|1.90:1
|
|
|Yes
|-
|Belgium
|[[Brussels]]
|Kinepolis Brussels & IMAX
|1.43:1
|IMAX GT Laser
|1.43:1
|[[70 mm film#IMAX%20(15/70)|15/70 mm]]
|27.6 m × 19.3 m
90.6 ft × 63.3 ft 533 m<sup>2</sup>
5,740 sq ft
|Yes
|}
"""

# Americas-style layout: 10 columns, with a nested rowspan inside a rowspan.
AMERICAS = """== Americas ==
{| class="sortable fandom-table"
!Country
! State
!City
!Location Name

!Screen Aspect Ratio (AR)
!Digital Projector
!Maximum AR for digital projection
!Film Projector
!Screen dimensions
!Commercial films shown?
|-
| rowspan="3" |Brazil
| rowspan="2" |São Paulo
|Campinas
|Kinoplex D. Pedro
|
|
|
|
|
|
|-
|[[São Paulo]]
|Cinépolis Iguatemi & IMAX
|1.90:1
|IMAX Cola
|1.90:1
|
|19.10m×10.60m
|Yes
|-
|Bahia
|Salvador
|UCI Orient Shopping da Bahia
|
|
|
|
|
|No
|-
|Canada
| rowspan="1" |AB
|[[Calgary]]
|Scotiabank Chinook & IMAX
|1.43:1
| IMAX Digital
|1.90:1
|IMAX SR 15/70 mm, (2D Only)(Temporary)
|21.33m×16.18m
|Yes
|}
"""


def records(wikitext):
    recs, per_region, warnings = sync.parse_page(wikitext)
    return {r["name"]: r for r in recs}, per_region, warnings


class TestCleaning(unittest.TestCase):
    def test_strips_piped_link_keeping_label(self):
        self.assertEqual(sync.clean("[[70 mm film#IMAX%20(15/70)|15/70 mm]]"), "15/70 mm")

    def test_strips_bare_link(self):
        self.assertEqual(sync.clean("[[Nassau, Bahamas]]"), "Nassau, Bahamas")

    def test_converts_superscript_two_and_entities(self):
        self.assertEqual(sync.clean("533 m<sup>2</sup> &amp; more"), "533 m² & more")

    def test_clean_line_folds_wrapped_content(self):
        self.assertEqual(sync.clean_line("17.4 m × 9.2 m\n57.1 ft"), "17.4 m × 9.2 m 57.1 ft")


class TestDimensions(unittest.TestCase):
    def test_spaced_metric_pair(self):
        self.assertEqual(sync._parse_dimensions("17.4 m × 9.2 m\n57.1 ft × 30.2 ft"),
                         (17.4, 9.2, 160.1))

    def test_unspaced_metric_pair(self):
        self.assertEqual(sync._parse_dimensions("19.10m×10.60m"), (19.1, 10.6, 202.5))

    def test_explicit_area_wins_over_product(self):
        self.assertEqual(sync._parse_dimensions("27.6 m × 19.3 m\n533 m²"),
                         (27.6, 19.3, 533.0))

    def test_decimal_comma_area_is_not_read_as_thousands(self):
        # German rows: "203,8 m²" is 203.8, not 2038.  The imperial figure on the
        # second line uses the comma the other way round, in the same cell.
        self.assertEqual(
            sync._parse_dimensions(
                "20.0 m × 10.19 m (203,8 m²)\n65.62 ft × 33.43 ft (2,194 sq ft)"),
            (20.0, 10.19, 203.8))

    def test_thousands_separator_in_metric_area(self):
        self.assertEqual(sync._parse_dimensions("40 m × 30 m\n1,200 m²"),
                         (40.0, 30.0, 1200.0))

    def test_dome_single_diameter(self):
        self.assertEqual(sync._parse_dimensions("23 m"), (23.0, None, None))

    def test_zero_height_is_unknown_not_zero(self):
        # Two rows are written "21.0 m × 0 m": the height is unknown, and
        # storing 0.0 would render as a screen 21 m wide and nothing tall.
        self.assertEqual(
            sync._parse_dimensions("21.0 m × 0 m\n68.9 ft × 0.00 ft"),
            (21.0, None, None))

    def test_empty(self):
        self.assertEqual(sync._parse_dimensions(""), (None, None, None))

    def test_inconsistent_area_is_flagged_in_notes(self):
        record = sync.build_record("Europe", {
            "country": "Testland", "city": "Testville", "name": "Test IMAX",
            "dimensions_raw": "20 m × 10 m\n900 m²",
        })
        self.assertIn("disagrees", record["data_notes"])

    def test_consistent_area_is_not_flagged(self):
        record = sync.build_record("Europe", {
            "country": "Testland", "city": "Testville", "name": "Test IMAX",
            "dimensions_raw": "20.0 m × 10.19 m (203,8 m²)",
        })
        self.assertEqual(record["data_notes"], "")


class TestAspectRatio(unittest.TestCase):
    def test_colon_typo_becomes_decimal_point(self):
        # Three Vietnamese rows write "1:90:1"; the LIKE '1.90%' filter in
        # serve.py hid them from the 1.90 list entirely.
        self.assertEqual(sync._normalize_ar("1:90:1"), "1.90:1")

    def test_well_formed_ratios_are_untouched(self):
        for ar in ("1.90:1", "1.43:1", "Dome 1.43:1", ""):
            with self.subTest(ar=ar):
                self.assertEqual(sync._normalize_ar(ar), ar)

    def _flag(self, ar):
        return sync.build_record("Europe", {
            "country": "Testland", "city": "Testville", "name": "Test IMAX",
            "screen_ar": ar,
        })["is_1_43"]

    def test_dome_1_43_counts_as_1_43(self):
        # The 28 dome venues are written "Dome 1.43:1".  Anchoring the flag at
        # the start of the string excluded every one of them from the filter.
        self.assertEqual(self._flag("Dome 1.43:1"), 1)

    def test_plain_1_43_counts(self):
        self.assertEqual(self._flag("1.43:1"), 1)

    def test_1_90_does_not_count(self):
        self.assertEqual(self._flag("1.90:1"), 0)

    def test_typo_is_normalized_into_the_record(self):
        record = sync.build_record("Asia", {
            "country": "Vietnam", "city": "District 10", "name": "CGV & IMAX",
            "screen_ar": "1:90:1",
        })
        self.assertEqual(record["screen_ar"], "1.90:1")


class TestProjectorNormalization(unittest.TestCase):
    def test_every_observed_cola_spelling_folds_together(self):
        for spelling in ("IMAX CoLa", "IMAX Cola", "IMAX CoLA", "IMAX Laser CoLa"):
            with self.subTest(spelling=spelling):
                self.assertEqual(sync._classify(spelling, sync.DIGITAL_FAMILIES),
                                 ("CoLa", "IMAX CoLa"))

    def test_xt_in_either_word_order(self):
        for spelling in ("IMAX Laser XT", "IMAX XT Laser"):
            with self.subTest(spelling=spelling):
                self.assertEqual(sync._classify(spelling, sync.DIGITAL_FAMILIES)[0], "XT")

    def test_dome_laser_variants(self):
        for spelling in ("IMAX Laser for Dome", "IMAX Dome with Laser"):
            with self.subTest(spelling=spelling):
                self.assertEqual(sync._classify(spelling, sync.DIGITAL_FAMILIES)[0],
                                 "Dome Laser")

    def test_gt_laser_beats_bare_laser(self):
        self.assertEqual(sync._classify("IMAX GT Laser", sync.DIGITAL_FAMILIES)[0], "GT Laser")

    def test_film_families(self):
        cases = {
            "IMAX GT3D 15/70 mm": "GT3D",
            "IMAX GT Dome 15/70 mm": "GT Dome",
            "IMAX SR Dome 15/70 mm": "SR Dome",
            "IMAX SR 15/70mm": "SR",
            "IMAX Dome 15/70 mm 3D": "Dome",
        }
        for raw, family in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(sync._classify(raw, sync.FILM_FAMILIES)[0], family)


class TestEuropeLayout(unittest.TestCase):
    def setUp(self):
        self.venues, self.regions, self.warnings = records(EUROPE)

    def test_row_count_and_region(self):
        self.assertEqual(self.regions, {"Europe": 3})
        self.assertEqual(self.warnings, [])

    def test_rowspan_country_carries_to_next_row(self):
        self.assertEqual(self.venues["CineplexX Apollo Vienna & IMAX"]["country"], "Austria")
        self.assertEqual(self.venues["CineplexX Apollo Vienna & IMAX"]["city"], "Vienna")

    def test_case_variant_normalized(self):
        self.assertEqual(self.venues["CineplexX Apollo Vienna & IMAX"]["digital_projector"],
                         "IMAX CoLa")

    def test_film_venue_flags_and_area(self):
        v = self.venues["Kinepolis Brussels & IMAX"]
        self.assertEqual(v["has_70mm"], 1)
        self.assertEqual(v["is_1_43"], 1)
        self.assertEqual(v["film_family"], "15/70")
        self.assertEqual(v["screen_area_m2"], 533.0)
        self.assertEqual(v["commercial_films"], 1)

    def test_maps_url_uses_name_city_country(self):
        self.assertEqual(
            self.venues["Kinepolis Brussels & IMAX"]["maps_url"],
            "https://www.google.com/maps/search/?api=1&query="
            "Kinepolis+Brussels+%26+IMAX%2C+Brussels%2C+Belgium",
        )


class TestAmericasLayout(unittest.TestCase):
    def setUp(self):
        self.venues, self.regions, self.warnings = records(AMERICAS)

    def test_row_count(self):
        self.assertEqual(self.regions, {"Americas": 4})
        self.assertEqual(self.warnings, [])

    def test_nested_rowspan_state_inside_country(self):
        # Brazil spans 3 rows; São Paulo spans the first 2 of them.
        self.assertEqual(self.venues["Kinoplex D. Pedro"]["state"], "São Paulo")
        self.assertEqual(self.venues["Cinépolis Iguatemi & IMAX"]["state"], "São Paulo")
        self.assertEqual(self.venues["Cinépolis Iguatemi & IMAX"]["country"], "Brazil")
        # ...and the third Brazil row picks up its own state again.
        self.assertEqual(self.venues["UCI Orient Shopping da Bahia"]["state"], "Bahia")
        self.assertEqual(self.venues["UCI Orient Shopping da Bahia"]["country"], "Brazil")

    def test_state_column_does_not_leak_into_city(self):
        self.assertEqual(self.venues["Kinoplex D. Pedro"]["city"], "Campinas")
        self.assertEqual(self.venues["Scotiabank Chinook & IMAX"]["city"], "Calgary")

    def test_blank_row_yields_empty_fields_not_a_crash(self):
        v = self.venues["Kinoplex D. Pedro"]
        self.assertEqual(v["digital_projector"], "")
        self.assertEqual(v["has_70mm"], 0)
        self.assertIsNone(v["commercial_films"])

    def test_annotations_extracted(self):
        v = self.venues["Scotiabank Chinook & IMAX"]
        self.assertEqual(v["has_70mm"], 1)
        self.assertEqual(v["film_family"], "SR")
        self.assertEqual(v["is_temporary"], 1)
        self.assertIn("2D Only", v["data_notes"])

    def test_no_commercial_films_reads_as_zero_not_null(self):
        self.assertEqual(self.venues["UCI Orient Shopping da Bahia"]["commercial_films"], 0)

    def test_diacritics_stripped_from_key(self):
        self.assertEqual(self.venues["Cinépolis Iguatemi & IMAX"]["venue_key"],
                         "brazil|saopaulo|saopaulo|cinepolisiguatemiimax")


class TestMisfiledAndDuplicates(unittest.TestCase):
    def test_digital_value_in_film_column_is_rerouted(self):
        record = sync.build_record("Europe", {
            "country": "Kosovo", "city": "Prishtina", "name": "Cineplex",
            "digital_raw": "", "film_raw": "IMAX XT Laser",
        })
        self.assertEqual(record["has_70mm"], 0)
        self.assertEqual(record["film_family"], "")
        self.assertEqual(record["projector_family"], "XT")
        self.assertIn("film column held a digital projector", record["data_notes"])

    def test_duplicate_keys_are_suffixed_and_warned(self):
        doubled = EUROPE + EUROPE.replace("== Europe ==", "== Europe 2 ==")
        recs, _, warnings = sync.parse_page(doubled)
        keys = [r["venue_key"] for r in recs]
        self.assertEqual(len(keys), len(set(keys)))
        self.assertEqual(len(warnings), 3)

    def test_missing_required_column_raises(self):
        broken = EUROPE.replace("!Location Name", "!Something Else")
        with self.assertRaises(sync.ParseError):
            sync.parse_page(broken)

    def test_unknown_extra_column_warns_but_parses(self):
        extended = EUROPE.replace("!Country\n", "!Country\n!Snack Bar\n")
        recs, _, warnings = sync.parse_page(extended)
        self.assertTrue(any("Snack Bar" in w for w in warnings))
        self.assertEqual(len(recs), 3)


if __name__ == "__main__":
    unittest.main(verbosity=2)
