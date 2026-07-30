#!/usr/bin/env python3
"""Tests for the database layer of sync.py: apply_revision and validate.

These cover the guarantees the design rests on but that the parser tests never
touch - that a venue disappearing upstream is soft-deleted rather than dropped,
that every field-level edit lands in venue_changes, and that a sync which leaves
the database inconsistent says so.

Everything runs against a throwaway SQLite file; nothing here touches the network.

    python3 test_db.py
"""

import sqlite3
import tempfile
import unittest
from pathlib import Path

import sync

REV1_AT = "2026-01-01T00:00:00+00:00"
REV2_AT = "2026-02-01T00:00:00+00:00"


def venue(name="Test IMAX", city="Testville", country="Testland", **overrides):
    """Build one normalized record the way parse_page would."""
    fields = {"country": country, "city": city, "name": name}
    fields.update(overrides)
    return sync.build_record(overrides.pop("region", "Europe"), fields)


class DBCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "test.sqlite3"
        self.conn = sync.connect(self.db)
        self.addCleanup(self.conn.close)

    def apply(self, records, revid=1, fetched_at=REV1_AT):
        return sync.apply_revision(self.conn, records, revid, "2026-01-01T00:00:00Z",
                                   fetched_at, "sha", "snap.wiki")

    def rows(self, sql, params=()):
        return self.conn.execute(sql, params).fetchall()

    def one(self, sql, params=()):
        return self.conn.execute(sql, params).fetchone()[0]


class TestFirstRevision(DBCase):
    def test_inserts_and_stamps_provenance(self):
        result = self.apply([venue("A"), venue("B", city="Otherville")])
        self.assertEqual((result["added"], result["changed"], result["removed"]),
                         (2, 0, 0))
        row = self.one("SELECT count(*) FROM venues")
        self.assertEqual(row, 2)
        a = self.rows("SELECT * FROM venues WHERE name = 'A'")[0]
        self.assertEqual(a["first_seen_revid"], 1)
        self.assertEqual(a["last_seen_revid"], 1)
        self.assertEqual(a["first_seen_at"], REV1_AT)
        self.assertIsNone(a["removed_at"])

    def test_records_the_revision(self):
        self.apply([venue("A")])
        rev = self.rows("SELECT * FROM revisions")[0]
        self.assertEqual(rev["revid"], 1)
        self.assertEqual(rev["venue_count"], 1)
        self.assertEqual(rev["n_added"], 1)

    def test_new_venues_are_searchable(self):
        self.apply([venue("A", city="Vancouver")])
        hit = self.one("SELECT count(*) FROM venues_fts WHERE venues_fts MATCH ?",
                       ('"vancou"*',))
        self.assertEqual(hit, 1)


class TestUnchangedRevision(DBCase):
    def test_reapplying_identical_data_logs_nothing(self):
        records = [venue("A"), venue("B", city="Otherville")]
        self.apply(records)
        result = self.apply(records, revid=2, fetched_at=REV2_AT)
        self.assertEqual((result["added"], result["changed"], result["removed"]),
                         (0, 0, 0))
        self.assertEqual(self.one("SELECT count(*) FROM venue_changes"), 0)

    def test_last_seen_advances_even_without_edits(self):
        records = [venue("A")]
        self.apply(records)
        self.apply(records, revid=2, fetched_at=REV2_AT)
        row = self.rows("SELECT * FROM venues WHERE name = 'A'")[0]
        self.assertEqual(row["first_seen_revid"], 1)
        self.assertEqual(row["last_seen_revid"], 2)

    def test_float_noise_does_not_log_a_phantom_change(self):
        # 533.0 and 533.00000000001 are the same screen; _normalize_for_compare
        # exists so a re-parse does not spam the change log.
        self.apply([venue("A", dimensions_raw="27.6 m × 19.3 m\n533 m²")])
        self.conn.execute("UPDATE venues SET screen_area_m2 = 533.00000000001")
        self.conn.commit()
        self.apply([venue("A", dimensions_raw="27.6 m × 19.3 m\n533 m²")], revid=2)
        self.assertEqual(self.one("SELECT count(*) FROM venue_changes"), 0)


class TestChangedRevision(DBCase):
    def setUp(self):
        super().setUp()
        self.apply([venue("A", screen_ar="1.90:1")])

    def test_edit_is_applied_and_logged(self):
        result = self.apply([venue("A", screen_ar="1.43:1")], revid=2,
                            fetched_at=REV2_AT)
        self.assertEqual(result["changed"], 1)
        self.assertEqual(self.one("SELECT screen_ar FROM venues"), "1.43:1")

        changes = {r["field"]: (r["old_value"], r["new_value"])
                   for r in self.rows("SELECT * FROM venue_changes")}
        self.assertEqual(changes["screen_ar"], ("1.90:1", "1.43:1"))
        # The derived flag moved with it, and that is logged too.
        self.assertEqual(changes["is_1_43"], ("0", "1"))

    def test_change_rows_carry_the_revision_that_caused_them(self):
        self.apply([venue("A", screen_ar="1.43:1")], revid=2, fetched_at=REV2_AT)
        self.assertEqual(self.one("SELECT DISTINCT revid FROM venue_changes"), 2)

    def test_renaming_a_venue_creates_a_new_row(self):
        # venue_key is built from country/state/city/name, so a rename reads as
        # one venue leaving and another arriving.  Both survive.
        result = self.apply([venue("A Renamed", screen_ar="1.90:1")], revid=2,
                            fetched_at=REV2_AT)
        self.assertEqual((result["added"], result["removed"]), (1, 1))
        self.assertEqual(self.one("SELECT count(*) FROM venues"), 2)


class TestSoftDelete(DBCase):
    def setUp(self):
        super().setUp()
        self.apply([venue("A"), venue("B", city="Otherville")])

    def test_vanished_venue_is_marked_not_deleted(self):
        result = self.apply([venue("A")], revid=2, fetched_at=REV2_AT)
        self.assertEqual(result["removed"], 1)
        # The whole design rests on this: history survives a bad wiki edit.
        self.assertEqual(self.one("SELECT count(*) FROM venues"), 2)
        b = self.rows("SELECT * FROM venues WHERE name = 'B'")[0]
        self.assertEqual(b["removed_at"], REV2_AT)

    def test_removal_is_logged_as_a_status_change(self):
        self.apply([venue("A")], revid=2, fetched_at=REV2_AT)
        row = self.rows("SELECT * FROM venue_changes WHERE field = '_status'")[0]
        self.assertEqual((row["old_value"], row["new_value"]), ("present", "removed"))

    def test_removed_names_are_reported(self):
        result = self.apply([venue("A")], revid=2, fetched_at=REV2_AT)
        self.assertEqual(result["removed_names"], ["B (Otherville, Testland)"])

    def test_already_removed_venue_is_not_removed_twice(self):
        self.apply([venue("A")], revid=2, fetched_at=REV2_AT)
        result = self.apply([venue("A")], revid=3, fetched_at=REV2_AT)
        self.assertEqual(result["removed"], 0)
        self.assertEqual(
            self.one("SELECT count(*) FROM venue_changes WHERE field = '_status'"), 1)

    def test_reappearing_venue_is_restored(self):
        self.apply([venue("A")], revid=2, fetched_at=REV2_AT)
        self.apply([venue("A"), venue("B", city="Otherville")], revid=3,
                   fetched_at=REV2_AT)
        b = self.rows("SELECT * FROM venues WHERE name = 'B'")[0]
        self.assertIsNone(b["removed_at"])
        restored = self.rows(
            "SELECT * FROM venue_changes WHERE field = '_status'"
            " ORDER BY id DESC LIMIT 1")[0]
        self.assertEqual((restored["old_value"], restored["new_value"]),
                         ("removed", "present"))


class TestRevisionBookkeeping(DBCase):
    def test_force_reapplying_a_revision_upserts_rather_than_duplicating(self):
        self.apply([venue("A")])
        self.apply([venue("A"), venue("B", city="Otherville")], revid=1,
                   fetched_at=REV2_AT)
        self.assertEqual(self.one("SELECT count(*) FROM revisions"), 1)
        rev = self.rows("SELECT * FROM revisions")[0]
        self.assertEqual(rev["venue_count"], 2)
        self.assertEqual(rev["fetched_at"], REV2_AT)

    def test_latest_revision_picks_the_newest(self):
        self.apply([venue("A")])
        self.apply([venue("A")], revid=2, fetched_at=REV2_AT)
        self.assertEqual(sync.latest_revision(self.conn)["revid"], 2)


class TestValidate(DBCase):
    def setUp(self):
        super().setUp()
        self.apply([
            venue("Flat IMAX", screen_ar="1.43:1",
                  film_raw="IMAX GT3D 15/70 mm",
                  dimensions_raw="27.6 m × 19.3 m\n533 m²"),
            venue("Dome IMAX", city="Otherville", screen_ar="Dome 1.43:1",
                  digital_raw="IMAX Dome Laser"),
        ])

    def checks(self):
        return {f["check"]: f for f in sync.validate(self.conn)}

    def test_clean_database_passes(self):
        self.assertEqual(sync.validate(self.conn), [])

    def test_dome_1_43_does_not_trip_the_flag_check(self):
        # Regression guard for the bug where is_1_43 excluded every dome.
        self.assertNotIn("ar_1_43_flag_disagrees", self.checks())

    def test_detects_a_flag_that_stopped_matching_its_text(self):
        self.conn.execute("UPDATE venues SET is_1_43 = 0 WHERE name = 'Dome IMAX'")
        self.conn.commit()
        finding = self.checks()["ar_1_43_flag_disagrees"]
        self.assertEqual(finding["severity"], "WARN")
        self.assertEqual(finding["examples"], ["Dome IMAX"])

    def test_detects_a_broken_fts_trigger(self):
        # If the insert trigger is ever lost, search silently returns fewer
        # venues than the table holds and nothing else gives it away.
        self.conn.execute("DROP TRIGGER venues_fts_ai")
        self.conn.commit()
        self.apply([venue("Ghost IMAX", city="Nowhere")], revid=2)
        finding = self.checks()["fts_index_drift"]
        self.assertEqual(finding["severity"], "ERROR")

    def test_detects_coordinates_without_a_precision(self):
        # Coordinates and the confidence they were recorded with have to travel
        # together; a bare lat/lon says nothing about whether it is the theatre
        # or just the middle of the town.
        self.conn.execute("UPDATE venues SET lat = 51.5, lon = -0.1"
                          " WHERE name = 'Flat IMAX'")
        self.conn.commit()
        finding = self.checks()["geocode_inconsistent"]
        self.assertEqual(finding["severity"], "ERROR")

    def test_detects_a_precision_without_coordinates(self):
        self.conn.execute("UPDATE venues SET geo_precision = 'venue'"
                          " WHERE name = 'Flat IMAX'")
        self.conn.commit()
        self.assertIn("geocode_inconsistent", self.checks())

    def test_detects_impossible_coordinates(self):
        self.conn.execute("UPDATE venues SET lat = 950, lon = -0.1,"
                          " geo_precision = 'venue' WHERE name = 'Flat IMAX'")
        self.conn.commit()
        self.assertIn("coordinates_out_of_range", self.checks())

    def test_a_properly_geocoded_venue_passes(self):
        # "Properly" now includes recording what OSM matched: coordinates
        # without it cannot be audited later without re-querying, which is what
        # the geocode_unaudited check exists to prevent.
        self.conn.execute("UPDATE venues SET lat = 51.5074, lon = -0.1278,"
                          " geo_precision = 'venue', geo_source = 'nominatim',"
                          " geo_matched = 'BFI IMAX, Waterloo, London'"
                          " WHERE name = 'Flat IMAX'")
        self.conn.commit()
        self.assertEqual(sync.validate(self.conn), [])

    def test_a_sync_does_not_overwrite_coordinates(self):
        # Geocoding is not wiki data; re-syncing the page must leave it alone.
        self.conn.execute("UPDATE venues SET lat = 51.5074, lon = -0.1278,"
                          " geo_precision = 'venue' WHERE name = 'Flat IMAX'")
        self.conn.commit()
        self.apply([
            venue("Flat IMAX", screen_ar="1.43:1",
                  film_raw="IMAX GT3D 15/70 mm",
                  dimensions_raw="27.6 m × 19.3 m\n533 m²"),
        ], revid=2)
        row = self.rows("SELECT * FROM venues WHERE name = 'Flat IMAX'")[0]
        self.assertEqual(row["lat"], 51.5074)
        self.assertEqual(row["geo_precision"], "venue")

    def test_detects_implausible_dimensions(self):
        self.conn.execute("UPDATE venues SET screen_h_m = 0 WHERE name = 'Flat IMAX'")
        self.conn.commit()
        self.assertIn("implausible_dimensions", self.checks())

    def test_detects_a_missing_required_field(self):
        self.conn.execute("UPDATE venues SET maps_url = '' WHERE name = 'Flat IMAX'")
        self.conn.commit()
        finding = self.checks()["missing_required_field"]
        self.assertEqual(finding["severity"], "ERROR")

    def test_detects_an_unknown_region(self):
        self.conn.execute("UPDATE venues SET region = 'Atlantis'")
        self.conn.commit()
        self.assertIn("unknown_region", self.checks())

    def test_detects_a_bad_commercial_films_value(self):
        self.conn.execute("UPDATE venues SET commercial_films = 7")
        self.conn.commit()
        self.assertIn("bad_commercial_films", self.checks())

    def test_null_commercial_films_is_allowed(self):
        self.conn.execute("UPDATE venues SET commercial_films = NULL")
        self.conn.commit()
        self.assertNotIn("bad_commercial_films", self.checks())

    def test_detects_a_70mm_venue_with_no_projector_model(self):
        self.conn.execute("UPDATE venues SET film_family = '' WHERE has_70mm = 1")
        self.conn.commit()
        self.assertIn("film70_without_family", self.checks())

    def test_findings_cap_their_examples(self):
        self.apply([venue(f"Venue {i}", city=f"City {i}") for i in range(10)],
                   revid=2)
        self.conn.execute("UPDATE venues SET region = 'Atlantis'")
        self.conn.execute("UPDATE venues SET maps_url = ''")
        self.conn.commit()
        self.assertLessEqual(len(self.checks()["missing_required_field"]["examples"]), 5)


if __name__ == "__main__":
    unittest.main(verbosity=2)
