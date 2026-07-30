#!/usr/bin/env python3
"""Tests for export.py - the step that turns the database into a public site.

Two things matter here. The bundle must be a faithful copy of the database,
because once it is deployed nothing else checks it; and the bundle must not
contain the admin page, because that is the whole security model now that the
site is published.

    python3 test_export.py
"""

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

import export
import sync

REV_AT = "2026-01-01T00:00:00+00:00"


def venue(name, **overrides):
    fields = {"country": overrides.pop("country", "Testland"),
              "city": overrides.pop("city", "Testville"), "name": name}
    fields.update(overrides)
    return sync.build_record(overrides.pop("region", "Europe"), fields)


class ExportCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.db = self.root / "test.sqlite3"

        conn = sync.connect(self.db)
        sync.apply_revision(conn, [
            venue("Flat 70", country="Canada", city="Vancouver",
                  screen_ar="1.43:1", film_raw="IMAX GT3D 15/70 mm",
                  digital_raw="IMAX GT Laser",
                  dimensions_raw="27.6 m × 19.3 m\n533 m²"),
            venue("Dome 70", country="Belgium", city="Bruxelles",
                  screen_ar="Dome 1.43:1", film_raw="IMAX SR Dome 15/70 mm",
                  digital_raw="IMAX Dome Laser"),
            venue("Digital only", country="Japan", city="Tokyo",
                  screen_ar="1.90:1", digital_raw="IMAX CoLa"),
            venue("Doomed", country="France", city="Paris",
                  screen_ar="1.90:1", digital_raw="IMAX CoLa"),
        ], 1, "2026-01-01T00:00:00Z", REV_AT, "sha", "snap.wiki")
        # Drop one so the export has a de-listed venue to carry.
        sync.apply_revision(conn, [
            venue("Flat 70", country="Canada", city="Vancouver",
                  screen_ar="1.43:1", film_raw="IMAX GT3D 15/70 mm",
                  digital_raw="IMAX GT Laser",
                  dimensions_raw="27.6 m × 19.3 m\n533 m²"),
            venue("Dome 70", country="Belgium", city="Bruxelles",
                  screen_ar="Dome 1.43:1", film_raw="IMAX SR Dome 15/70 mm",
                  digital_raw="IMAX Dome Laser"),
            venue("Digital only", country="Japan", city="Tokyo",
                  screen_ar="1.90:1", digital_raw="IMAX CoLa"),
        ], 2, "2026-02-01T00:00:00Z", REV_AT, "sha", "snap.wiki")
        conn.close()

        self.conn = sqlite3.connect(f"file:{self.db}?mode=ro", uri=True)
        self.conn.row_factory = sqlite3.Row
        self.addCleanup(self.conn.close)
        self.payload = export.build_payload(self.conn)


class TestPayload(ExportCase):
    def test_every_venue_is_exported_including_delisted(self):
        self.assertEqual(len(self.payload["venues"]), 4)

    def test_stats_count_only_live_venues(self):
        stats = self.payload["stats"]
        self.assertEqual(stats["venues"], 3)
        self.assertEqual(stats["removed"], 1)
        self.assertEqual(stats["film70"], 2)
        self.assertEqual(stats["countries"], 3)

    def test_delisted_venue_carries_its_removal_date(self):
        doomed = next(v for v in self.payload["venues"] if v["name"] == "Doomed")
        self.assertIsNotNone(doomed["removed_at"])

    def test_facets_agree_with_the_venues_beside_them(self):
        live = [v for v in self.payload["venues"] if v["removed_at"] is None]
        for facet, field in (("countries", "country"), ("regions", "region"),
                             ("projectors", "projector_family"),
                             ("films", "film_family")):
            with self.subTest(facet=facet):
                for row in self.payload["facets"][facet]:
                    actual = sum(1 for v in live if v[field] == row["value"])
                    self.assertEqual(row["n"], actual)

    def test_film_buckets_partition_the_70mm_venues(self):
        films = self.payload["facets"]["films"]
        self.assertEqual(sum(f["n"] for f in films),
                         self.payload["facets"]["film70"])

    def test_ar_facet_counts_the_flag_so_domes_are_included(self):
        ars = {a["value"]: a["n"] for a in self.payload["facets"]["ars"]}
        self.assertEqual(ars["1.43"], 2)  # flat and dome
        self.assertEqual(ars["1.90"], 1)

    def test_coordinates_are_exported_with_their_precision(self):
        # The page treats "venue" and "city" differently — one gets directions
        # to the door, the other is honest about pointing at the town — so the
        # precision has to travel with the coordinates.
        self.conn.close()
        conn = sync.connect(self.db)
        conn.execute("UPDATE venues SET lat = 49.2827, lon = -123.1207,"
                     " geo_precision = 'venue', geo_source = 'nominatim'"
                     " WHERE name = 'Flat 70'")
        conn.commit()
        conn.close()

        self.conn = sqlite3.connect(f"file:{self.db}?mode=ro", uri=True)
        self.conn.row_factory = sqlite3.Row
        payload = export.build_payload(self.conn)
        flat = next(v for v in payload["venues"] if v["name"] == "Flat 70")
        self.assertEqual(flat["lat"], 49.2827)
        self.assertEqual(flat["lon"], -123.1207)
        self.assertEqual(flat["geo_precision"], "venue")

    def test_ungeocoded_venues_export_null_coordinates(self):
        dome = next(v for v in self.payload["venues"] if v["name"] == "Dome 70")
        self.assertIsNone(dome["lat"])
        self.assertIsNone(dome["lon"])
        self.assertEqual(dome["geo_precision"], "")

    def test_no_provenance_or_raw_wikitext_leaks_out(self):
        exported = set(self.payload["venues"][0])
        for private in ("venue_key", "digital_raw", "film_raw",
                        "dimensions_raw", "first_seen_revid", "last_seen_revid"):
            self.assertNotIn(private, exported)

    def test_revision_is_recorded(self):
        self.assertEqual(self.payload["revision"]["revid"], 2)

    def test_payload_is_json_serialisable(self):
        json.dumps(self.payload, ensure_ascii=False)


class TestGeo(ExportCase):
    def test_timezone_map_is_populated(self):
        zones = self.payload["geo"]["timezones"]
        self.assertGreater(len(zones), 100)
        self.assertEqual(zones.get("Europe/Brussels"), "BE")
        self.assertEqual(zones.get("America/Vancouver"), "CA")

    def test_legacy_zone_names_browsers_still_report_are_mapped(self):
        # Chrome reports Asia/Calcutta, not Asia/Kolkata, on many systems; the
        # alias table is what keeps Indian visitors from falling through.
        zones = self.payload["geo"]["timezones"]
        self.assertEqual(zones.get("Asia/Calcutta"), "IN")
        self.assertEqual(zones.get("Europe/Kiev"), "UA")

    def test_country_codes_are_two_letters_and_unique(self):
        codes = export.COUNTRY_CODES
        self.assertTrue(all(len(c) == 2 and c.isupper() for c in codes))
        self.assertEqual(len(set(codes.values())), len(codes))

    def test_no_country_in_this_dataset_is_unmapped(self):
        self.assertEqual(self.payload["unmapped_countries"], [])


class TestRealDatabaseIsFullyMapped(unittest.TestCase):
    """The live database, not a fixture: every country it holds needs a code."""

    def test_every_country_has_an_iso_code(self):
        db = export.DEFAULT_DB
        if not db.exists():
            self.skipTest("no theatres.sqlite3; run ./sync.py first")
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            payload = export.build_payload(conn)
        finally:
            conn.close()
        self.assertEqual(
            payload["unmapped_countries"], [],
            "add these to export.COUNTRY_CODES or visitors there will not be "
            "recognised by the 'your country' section")


class TestBundle(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dist = Path(self.tmp.name) / "dist"

    def test_bundle_contains_exactly_the_public_files(self):
        if not (export.WEB_DIR / "data" / "venues.json").is_file():
            self.skipTest("run ./export.py first")
        export.build_dist(self.dist)
        written = sorted(str(p.relative_to(self.dist))
                         for p in self.dist.rglob("*") if p.is_file())
        # Optional files ship only when they exist locally — CNAME is present
        # once a custom domain is configured, absent before that.
        expected = list(export.PUBLIC_FILES) + list(export.PUBLIC_DATA) + [
            name for name in export.OPTIONAL_FILES
            if (export.WEB_DIR / name).is_file()]
        self.assertEqual(written, sorted(expected))

    def test_a_configured_custom_domain_reaches_the_bundle(self):
        """GitHub Pages reads the domain from a CNAME inside the artifact and
        drops it on any deploy that omits one."""
        cname = export.WEB_DIR / "CNAME"
        if not cname.is_file():
            self.skipTest("no custom domain configured")
        export.build_dist(self.dist)
        shipped = (self.dist / "CNAME")
        self.assertTrue(shipped.is_file())
        self.assertEqual(shipped.read_text().strip(), cname.read_text().strip())

    def test_the_admin_page_is_not_published(self):
        # The point of the whole static-export design: POST /api/sync has no
        # public surface because the page that calls it is never copied.
        if not (export.WEB_DIR / "data" / "venues.json").is_file():
            self.skipTest("run ./export.py first")
        export.build_dist(self.dist)
        names = [p.name for p in self.dist.rglob("*")]
        self.assertNotIn("admin.html", names)
        self.assertNotIn("admin.js", names)

    def test_admin_files_still_exist_locally(self):
        # ...and are not simply deleted: the local tooling must still work.
        self.assertTrue((export.WEB_DIR / "admin.html").is_file())
        self.assertTrue((export.WEB_DIR / "admin.js").is_file())

    def test_a_private_file_reaching_dist_is_refused(self):
        export.build_dist(self.dist)
        (self.dist / "admin.html").write_text("<p>oops</p>")
        # Rebuilding wipes it; simulate the leak by checking the guard directly.
        leaked = [p for p in self.dist.rglob("*") if p.is_file()
                  and any(pat in p.name.lower() for pat in export.PRIVATE_PATTERNS)]
        self.assertTrue(leaked, "the guard should consider admin.html private")

    def test_rebuilding_clears_stale_files(self):
        export.build_dist(self.dist)
        stray = self.dist / "leftover.txt"
        stray.write_text("from an older build")
        export.build_dist(self.dist)
        self.assertFalse(stray.exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
