-- FindMaxScreen schema.
--
-- One row per venue in `venues`, keyed by a normalized `venue_key` so repeated
-- syncs update in place.  Venues that vanish from the wiki are soft-deleted
-- (removed_at set) and never DELETEd, so history survives a bad upstream edit.
-- `revisions` records every fetch; `venue_changes` records every field-level
-- edit, which is what makes the dataset auditable over time.

PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS venues (
    id                INTEGER PRIMARY KEY,
    venue_key         TEXT    NOT NULL UNIQUE,

    -- location
    region            TEXT    NOT NULL DEFAULT '',
    country           TEXT    NOT NULL DEFAULT '',
    state             TEXT    NOT NULL DEFAULT '',
    city              TEXT    NOT NULL DEFAULT '',
    name              TEXT    NOT NULL DEFAULT '',
    maps_url          TEXT    NOT NULL DEFAULT '',

    -- projection
    screen_ar         TEXT    NOT NULL DEFAULT '',
    digital_projector TEXT    NOT NULL DEFAULT '',  -- normalized
    digital_raw       TEXT    NOT NULL DEFAULT '',  -- as written on the wiki
    projector_family  TEXT    NOT NULL DEFAULT '',  -- GT Laser|CoLa|XT|Dome Laser|Digital
    max_digital_ar    TEXT    NOT NULL DEFAULT '',
    film_projector    TEXT    NOT NULL DEFAULT '',  -- normalized
    film_raw          TEXT    NOT NULL DEFAULT '',
    film_family       TEXT    NOT NULL DEFAULT '',  -- GT|GT3D|SR|GT Dome|SR Dome|Dome|15/70

    -- derived filter flags
    has_70mm          INTEGER NOT NULL DEFAULT 0,
    is_dome           INTEGER NOT NULL DEFAULT 0,
    is_1_43           INTEGER NOT NULL DEFAULT 0,
    is_temporary      INTEGER NOT NULL DEFAULT 0,
    commercial_films  INTEGER,                      -- 1 yes / 0 no / NULL unknown

    -- screen size
    screen_w_m        REAL,
    screen_h_m        REAL,
    screen_area_m2    REAL,
    dimensions_raw    TEXT    NOT NULL DEFAULT '',

    -- location, geocoded separately from the wiki (which carries no coordinates)
    -- via OpenStreetMap/Nominatim.  ODbL, so storing these permanently is fine
    -- provided the attribution stays on the page.  Deliberately NOT in
    -- TRACKED_FIELDS: a wiki sync must never overwrite them.
    lat               REAL,
    lon               REAL,
    geo_source        TEXT    NOT NULL DEFAULT '',
    geo_precision     TEXT    NOT NULL DEFAULT '',  -- venue|city|none
    geocoded_at       TEXT,
    website           TEXT    NOT NULL DEFAULT '',  -- OSM website tag (ODbL)
    -- what OSM actually matched, so a stored point can be audited later
    -- without re-querying. Provenance only; not exported to the site.
    geo_matched       TEXT    NOT NULL DEFAULT '',

    -- provenance
    data_notes        TEXT    NOT NULL DEFAULT '',
    first_seen_revid  INTEGER,
    last_seen_revid   INTEGER,
    first_seen_at     TEXT,
    last_seen_at      TEXT,
    removed_at        TEXT
);

CREATE INDEX IF NOT EXISTS venues_country_idx  ON venues (country);
CREATE INDEX IF NOT EXISTS venues_70mm_idx     ON venues (has_70mm);
CREATE INDEX IF NOT EXISTS venues_family_idx   ON venues (projector_family);
CREATE INDEX IF NOT EXISTS venues_removed_idx  ON venues (removed_at);

-- Full-text search over the location columns only.  remove_diacritics 2 lets
-- "Sao Paulo" and "Liege" find "São Paulo" and "Liège".
CREATE VIRTUAL TABLE IF NOT EXISTS venues_fts USING fts5 (
    country, state, city, name,
    content = 'venues',
    content_rowid = 'id',
    tokenize = "unicode61 remove_diacritics 2"
);

CREATE TRIGGER IF NOT EXISTS venues_fts_ai AFTER INSERT ON venues BEGIN
    INSERT INTO venues_fts (rowid, country, state, city, name)
    VALUES (new.id, new.country, new.state, new.city, new.name);
END;

CREATE TRIGGER IF NOT EXISTS venues_fts_ad AFTER DELETE ON venues BEGIN
    INSERT INTO venues_fts (venues_fts, rowid, country, state, city, name)
    VALUES ('delete', old.id, old.country, old.state, old.city, old.name);
END;

CREATE TRIGGER IF NOT EXISTS venues_fts_au AFTER UPDATE ON venues BEGIN
    INSERT INTO venues_fts (venues_fts, rowid, country, state, city, name)
    VALUES ('delete', old.id, old.country, old.state, old.city, old.name);
    INSERT INTO venues_fts (rowid, country, state, city, name)
    VALUES (new.id, new.country, new.state, new.city, new.name);
END;

CREATE TABLE IF NOT EXISTS revisions (
    revid            INTEGER PRIMARY KEY,
    wiki_timestamp   TEXT NOT NULL,
    fetched_at       TEXT NOT NULL,
    wikitext_sha256  TEXT NOT NULL,
    snapshot_path    TEXT NOT NULL DEFAULT '',
    venue_count      INTEGER NOT NULL DEFAULT 0,
    n_added          INTEGER NOT NULL DEFAULT 0,
    n_removed        INTEGER NOT NULL DEFAULT 0,
    n_changed        INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS venue_changes (
    id         INTEGER PRIMARY KEY,
    venue_id   INTEGER NOT NULL REFERENCES venues (id),
    revid      INTEGER NOT NULL,
    field      TEXT    NOT NULL,
    old_value  TEXT,
    new_value  TEXT,
    changed_at TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS venue_changes_venue_idx ON venue_changes (venue_id);
CREATE INDEX IF NOT EXISTS venue_changes_revid_idx ON venue_changes (revid);
