import os
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

DB_PATH = Path(os.environ.get("EARWRYM_DB", "/data/earwrym.db"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS albums (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    mbid TEXT UNIQUE,
    title TEXT NOT NULL,
    artist TEXT NOT NULL,
    year INTEGER,
    track_count INTEGER,
    duration_seconds INTEGER,
    cover_art_url TEXT,
    wikipedia_blurb TEXT,
    genre_bucket TEXT DEFAULT 'Other',
    genre_tags TEXT,
    state TEXT NOT NULL DEFAULT 'to-listen',
    completion REAL DEFAULT 0.0,
    tracks_heard INTEGER DEFAULT 0,
    rym_rating REAL,
    rym_rated_at TEXT,
    navidrome_id TEXT,
    playlist_id TEXT,
    source TEXT,
    added_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    sort_order INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS listens (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    album_id INTEGER NOT NULL REFERENCES albums(id),
    track_name TEXT,
    listened_at TEXT NOT NULL,
    UNIQUE(album_id, track_name, listened_at)
);

CREATE TABLE IF NOT EXISTS rym_ratings_cache (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    rym_slug TEXT UNIQUE,
    artist TEXT,
    title TEXT,
    rating REAL,
    scraped_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS rym_wishlist_cache (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    rym_slug TEXT UNIQUE,
    artist TEXT,
    title TEXT,
    added_to_cache_at TEXT NOT NULL,
    sent_to_lidarr INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS stats (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    year INTEGER NOT NULL,
    albums_rated INTEGER DEFAULT 0,
    listening_time_seconds INTEGER DEFAULT 0,
    current_streak INTEGER DEFAULT 0,
    longest_streak INTEGER DEFAULT 0,
    last_listen_date TEXT,
    updated_at TEXT NOT NULL,
    UNIQUE(year)
);

CREATE TABLE IF NOT EXISTS playlists (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT UNIQUE NOT NULL,
    navidrome_playlist_id TEXT,
    genre_bucket TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS lidarr_pending (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    artist TEXT NOT NULL,
    title TEXT NOT NULL,
    album_mbid TEXT,
    cover_url TEXT,
    genre_bucket TEXT DEFAULT 'Other',
    lidarr_date TEXT,
    added_at TEXT NOT NULL,
    UNIQUE(artist, title)
);

CREATE TABLE IF NOT EXISTS discography_artists (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    artist_mbid TEXT UNIQUE,
    pinned INTEGER DEFAULT 0,
    total_releases INTEGER DEFAULT 0,
    completed_releases INTEGER DEFAULT 0,
    cover_url TEXT,
    added_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS discography_releases (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    artist_id INTEGER NOT NULL REFERENCES discography_artists(id),
    title TEXT NOT NULL,
    release_group_mbid TEXT,
    release_type TEXT,
    year INTEGER,
    status TEXT DEFAULT 'missing',
    album_id INTEGER REFERENCES albums(id),
    rym_rating REAL,
    added_at TEXT NOT NULL,
    UNIQUE(artist_id, release_group_mbid)
);

CREATE TABLE IF NOT EXISTS diary_entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    album_id INTEGER NOT NULL REFERENCES albums(id),
    listened_date TEXT NOT NULL,
    notes TEXT DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS album_tags (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    album_id INTEGER NOT NULL REFERENCES albums(id),
    tag TEXT NOT NULL,
    tag_type TEXT NOT NULL DEFAULT 'descriptor',
    source TEXT NOT NULL,
    weight INTEGER DEFAULT 1,
    fetched_at TEXT NOT NULL,
    UNIQUE(album_id, tag, source)
);

CREATE TABLE IF NOT EXISTS taste_profiles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT UNIQUE NOT NULL,
    tag_distribution TEXT,
    genre_distribution TEXT,
    rating_distribution TEXT,
    album_count INTEGER DEFAULT 0,
    computed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS recommendation_candidates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    artist_name TEXT NOT NULL,
    album_title TEXT NOT NULL,
    artist_mbid TEXT,
    release_group_mbid TEXT,
    release_mbid TEXT,
    year INTEGER,
    genre_tags TEXT,
    cover_art_url TEXT,
    source TEXT NOT NULL,
    source_score REAL DEFAULT 0.0,
    taste_score REAL DEFAULT 0.0,
    taste_scores_json TEXT,
    has_tags INTEGER DEFAULT 0,
    in_library INTEGER DEFAULT 0,
    dismissed INTEGER DEFAULT 0,
    discovered_at TEXT NOT NULL,
    UNIQUE(artist_name, album_title)
);

CREATE INDEX IF NOT EXISTS idx_rec_candidates_score ON recommendation_candidates(taste_score DESC);
CREATE INDEX IF NOT EXISTS idx_rec_candidates_source ON recommendation_candidates(source);


CREATE INDEX IF NOT EXISTS idx_album_tags_album ON album_tags(album_id);
CREATE INDEX IF NOT EXISTS idx_album_tags_tag ON album_tags(tag);
CREATE INDEX IF NOT EXISTS idx_album_tags_source ON album_tags(source);
CREATE INDEX IF NOT EXISTS idx_albums_state ON albums(state);
CREATE INDEX IF NOT EXISTS idx_albums_mbid ON albums(mbid);
CREATE INDEX IF NOT EXISTS idx_albums_search ON albums(title COLLATE NOCASE, artist COLLATE NOCASE);
CREATE INDEX IF NOT EXISTS idx_listens_album ON listens(album_id);
CREATE INDEX IF NOT EXISTS idx_rym_ratings_slug ON rym_ratings_cache(rym_slug);
CREATE INDEX IF NOT EXISTS idx_disco_artist_mbid ON discography_artists(artist_mbid);
CREATE INDEX IF NOT EXISTS idx_disco_releases_artist ON discography_releases(artist_id);
CREATE INDEX IF NOT EXISTS idx_diary_album ON diary_entries(album_id);
CREATE INDEX IF NOT EXISTS idx_diary_date ON diary_entries(listened_date DESC);
"""


_MIGRATIONS = [
    "ALTER TABLE recommendation_candidates ADD COLUMN release_type TEXT DEFAULT ''",
]


def init_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with get_db() as db:
        db.executescript(SCHEMA)
    with get_db() as db:
        for migration in _MIGRATIONS:
            try:
                db.execute(migration)
            except sqlite3.OperationalError:
                pass


@contextmanager
def get_db():
    conn = sqlite3.connect(str(DB_PATH), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def now_iso():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def get_album_by_mbid(mbid):
    with get_db() as db:
        return db.execute("SELECT * FROM albums WHERE mbid = ?", (mbid,)).fetchone()


def get_albums_by_state(state, limit=None):
    with get_db() as db:
        q = "SELECT * FROM albums WHERE state = ? ORDER BY sort_order, updated_at DESC"
        if limit:
            q += f" LIMIT {int(limit)}"
        return db.execute(q, (state,)).fetchall()


def upsert_album(mbid, title, artist, **kwargs):
    existing = get_album_by_mbid(mbid)
    ts = now_iso()
    if existing:
        sets = ", ".join(f"{k} = ?" for k in kwargs)
        vals = list(kwargs.values()) + [ts, mbid]
        with get_db() as db:
            db.execute(f"UPDATE albums SET {sets}, updated_at = ? WHERE mbid = ?", vals)
        return existing["id"]
    else:
        kwargs["mbid"] = mbid
        kwargs["title"] = title
        kwargs["artist"] = artist
        kwargs["added_at"] = ts
        kwargs["updated_at"] = ts
        cols = ", ".join(kwargs.keys())
        placeholders = ", ".join("?" for _ in kwargs)
        with get_db() as db:
            cur = db.execute(f"INSERT INTO albums ({cols}) VALUES ({placeholders})", list(kwargs.values()))
            return cur.lastrowid


def update_album_state(album_id, new_state):
    with get_db() as db:
        db.execute("UPDATE albums SET state = ?, updated_at = ? WHERE id = ?",
                   (new_state, now_iso(), album_id))


def delete_album(album_id):
    with get_db() as db:
        db.execute("DELETE FROM listens WHERE album_id = ?", (album_id,))
        db.execute("DELETE FROM albums WHERE id = ?", (album_id,))


def reorder_album(album_id, new_position):
    with get_db() as db:
        db.execute("UPDATE albums SET sort_order = ?, updated_at = ? WHERE id = ?",
                   (new_position, now_iso(), album_id))


def record_listen(album_id, track_name, listened_at):
    with get_db() as db:
        try:
            db.execute(
                "INSERT OR IGNORE INTO listens (album_id, track_name, listened_at) VALUES (?, ?, ?)",
                (album_id, track_name, listened_at)
            )
        except sqlite3.IntegrityError:
            pass


def get_listen_count(album_id):
    with get_db() as db:
        row = db.execute(
            "SELECT COUNT(DISTINCT track_name) as cnt FROM listens WHERE album_id = ?",
            (album_id,)
        ).fetchone()
        return row["cnt"] if row else 0


def get_year_stats(year):
    with get_db() as db:
        row = db.execute("SELECT * FROM stats WHERE year = ?", (year,)).fetchone()
        if not row:
            db.execute("INSERT INTO stats (year, updated_at) VALUES (?, ?)", (year, now_iso()))
            return db.execute("SELECT * FROM stats WHERE year = ?", (year,)).fetchone()
        return row


def increment_rated_count(year):
    with get_db() as db:
        db.execute(
            "UPDATE stats SET albums_rated = albums_rated + 1, updated_at = ? WHERE year = ?",
            (now_iso(), year)
        )
