import logging
import time
from datetime import datetime, timezone

from . import db
from .modules.listenbrainz import ListenBrainzClient, extract_album_info
from .modules.navidrome import NavidromeClient, find_album_in_library
from .modules.musicbrainz import (
    MusicBrainzClient, get_cover_art_url, get_wikipedia_blurb, match_genre_bucket
)
from .modules.rym import RYMClient
from .modules.lidarr import LidarrClient
from .modules.jellyfin import JellyfinClient
from .modules.one001albums import OneThousandOneClient, album_to_search_terms

log = logging.getLogger(__name__)


class EarwrymTracker:
    def __init__(self, config):
        self.config = config
        self.lb = ListenBrainzClient(config["listenbrainz"]["username"])
        self.nd = NavidromeClient(
            config["navidrome"]["url"],
            config["navidrome"]["username"],
            config["navidrome"]["password"],
        )
        self.mb = MusicBrainzClient(config.get("musicbrainz", {}).get(
            "user_agent", "Earwrym/1.0"
        ))

        if config.get("rym", {}).get("enabled"):
            self.rym = RYMClient(
                config["rym"]["username"],
                proxy_url=config.get("rym", {}).get("proxy_url"),
            )
            self._rym_scrape_enabled = config.get("rym", {}).get("scrape_enabled", True)
        else:
            self.rym = None
            self._rym_scrape_enabled = False

        if config.get("lidarr", {}).get("enabled"):
            self.lidarr = LidarrClient(
                config["lidarr"]["url"],
                config["lidarr"]["api_key"],
                config["lidarr"].get("quality_profile_id", 1),
                config["lidarr"].get("root_folder", "/music"),
            )
        else:
            self.lidarr = None

        if config.get("one_thousand_one_albums", {}).get("enabled"):
            self.gen1001 = OneThousandOneClient(
                config["one_thousand_one_albums"]["project_slug"]
            )
        else:
            self.gen1001 = None

        if config.get("jellyfin", {}).get("enabled"):
            self.jellyfin = JellyfinClient(
                config["jellyfin"]["url"],
                config["jellyfin"]["api_key"],
                config["jellyfin"]["user_id"],
                config["jellyfin"].get("music_library_id"),
            )
        else:
            self.jellyfin = None

        self._library_cache = {}
        self._library_cache_time = 0
        self._last_lb_ts = 0

    def refresh_library_cache(self):
        """Refresh the Navidrome library cache."""
        now = time.time()
        if now - self._library_cache_time < 3600:
            return
        log.info("Refreshing Navidrome library cache...")
        albums = self.nd.get_full_library()
        self._library_cache = {}
        for album in albums:
            key = f"{album.get('artist', '').lower()}|{album.get('name', '').lower()}"
            self._library_cache[key] = album
        self._library_cache_time = now
        log.info("Library cache: %d albums", len(self._library_cache))

    def is_in_library(self, artist, title):
        """Check if album exists in Navidrome library."""
        self.refresh_library_cache()
        key = f"{artist.lower()}|{title.lower()}"
        if key in self._library_cache:
            return self._library_cache[key]
        for cache_key, album in self._library_cache.items():
            if title.lower() in cache_key and artist.lower() in cache_key:
                return album
        return None

    def passes_filter(self, nd_album):
        """Check if album passes the min tracks / min duration filter."""
        cfg = self.config.get("album_filter", {})
        min_tracks = cfg.get("min_tracks", 4)
        min_duration = cfg.get("min_duration_seconds", 480)
        song_count = nd_album.get("songCount", 0)
        duration = nd_album.get("duration", 0)
        return song_count >= min_tracks or duration >= min_duration

    def poll_listens(self):
        """Main listen polling loop iteration."""
        log.debug("Polling ListenBrainz for recent listens...")
        listens = self.lb.get_recent_listens(count=50, min_ts=self._last_lb_ts or None)
        if not listens:
            return

        if listens:
            self._last_lb_ts = listens[0].get("listened_at", 0)

        albums_touched = set()
        for listen in listens:
            info = extract_album_info(listen)
            if not info["title"] or not info["artist"]:
                continue

            nd_album = self.is_in_library(info["artist"], info["title"])
            if not nd_album:
                continue
            if not self.passes_filter(nd_album):
                continue

            release_mbid = info["release_mbid"] or nd_album.get("musicBrainzId", "")
            if not release_mbid:
                release_mbid = f"nd:{nd_album.get('id', '')}"

            album_key = release_mbid
            if album_key in albums_touched:
                continue
            albums_touched.add(album_key)

            existing = db.get_album_by_mbid(release_mbid)
            if existing and existing["state"] in ("rated", "dismissed"):
                continue

            if not existing:
                existing = self._find_duplicate(info["artist"], info["title"])
            if existing and existing["state"] in ("rated", "dismissed"):
                continue

            if self._is_rated_on_rym(info["artist"], info["title"]):
                if existing:
                    self._mark_as_rated(existing["id"], info["artist"], info["title"])
                continue

            if not existing:
                album_id = self._create_album(release_mbid, info, nd_album)
                if not album_id:
                    continue
            else:
                album_id = existing["id"]

            db.record_listen(album_id, info["track_name"], info["listened_at"])
            self._update_completion(album_id, nd_album)

    def _create_album(self, release_mbid, listen_info, nd_album):
        """Create a new album entry with metadata."""
        dupe = self._find_duplicate(listen_info["artist"], listen_info["title"])
        if dupe:
            return dupe["id"]

        cover_url = get_cover_art_url(release_mbid)
        if not cover_url:
            cover_url = nd_album.get("coverArt", "")

        mb_meta = self.mb.get_album_metadata(release_mbid)
        genres = []
        wikipedia_url = None
        track_count = nd_album.get("songCount", 0)
        duration = nd_album.get("duration", 0)

        if mb_meta:
            genres = mb_meta.get("genres", [])
            wikipedia_url = mb_meta.get("wikipedia_url")
            if mb_meta.get("track_count"):
                track_count = mb_meta["track_count"]
            if mb_meta.get("duration_seconds"):
                duration = mb_meta["duration_seconds"]

        blurb = get_wikipedia_blurb(wikipedia_url) if wikipedia_url else None
        bucket = match_genre_bucket(genres, self.config.get("genre_buckets", []))

        return db.upsert_album(
            mbid=release_mbid,
            title=listen_info["title"],
            artist=listen_info["artist"],
            year=int(mb_meta["year"]) if mb_meta and mb_meta.get("year", "").isdigit() else None,
            track_count=track_count,
            duration_seconds=duration,
            cover_art_url=cover_url,
            wikipedia_blurb=blurb,
            genre_bucket=bucket,
            genre_tags=",".join(genres[:10]),
            state="listening",
            navidrome_id=nd_album.get("id", ""),
            source="scrobble",
        )

    def _update_completion(self, album_id, nd_album):
        """Recalculate album completion and update state if threshold met."""
        track_count = nd_album.get("songCount", 1)
        heard = db.get_listen_count(album_id)
        completion = min(heard / max(track_count, 1), 1.0)

        threshold = self.config.get("album_filter", {}).get("completion_threshold", 0.8)
        album = db.get_album_by_mbid(None)  # need to get by id
        with db.get_db() as conn:
            album = conn.execute("SELECT * FROM albums WHERE id = ?", (album_id,)).fetchone()

        if not album:
            return

        new_state = album["state"]
        if completion >= threshold and album["state"] == "listening":
            new_state = "listened-unrated"

        with db.get_db() as conn:
            conn.execute(
                "UPDATE albums SET completion = ?, tracks_heard = ?, state = ?, updated_at = ? WHERE id = ?",
                (completion, heard, new_state, db.now_iso(), album_id)
            )

    def match_cached_ratings(self):
        """Check all non-rated albums against the RYM ratings cache."""
        with db.get_db() as conn:
            non_rated = conn.execute(
                "SELECT * FROM albums WHERE state NOT IN ('rated', 'dismissed')"
            ).fetchall()
        matched = 0
        for album in non_rated:
            if self._is_rated_on_rym(album["artist"], album["title"]):
                self._mark_as_rated(album["id"], album["artist"], album["title"])
                matched += 1
        if matched:
            log.info("Matched %d albums from RYM cache", matched)

    def check_rym_ratings(self):
        """Scrape RYM for new ratings and update album states."""
        if not self.rym or not self._rym_scrape_enabled:
            return
        log.info("Checking RYM ratings...")
        ratings = self.rym.scrape_all_ratings()
        if not ratings:
            log.warning("No ratings returned from RYM scrape")
            return

        log.info("Found %d ratings on RYM", len(ratings))
        with db.get_db() as conn:
            for r in ratings:
                conn.execute(
                    "INSERT OR REPLACE INTO rym_ratings_cache (rym_slug, artist, title, rating, scraped_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (r["rym_slug"], r["artist"], r["title"], r["rating"], db.now_iso())
                )

        unrated = db.get_albums_by_state("listened-unrated")
        for album in unrated:
            if self._is_rated_on_rym(album["artist"], album["title"]):
                db.update_album_state(album["id"], "rated")
                rating = self._get_rym_rating(album["artist"], album["title"])
                with db.get_db() as conn:
                    conn.execute(
                        "UPDATE albums SET rym_rating = ?, rym_rated_at = ?, updated_at = ? WHERE id = ?",
                        (rating, db.now_iso(), db.now_iso(), album["id"])
                    )
                year = datetime.now(timezone.utc).year
                db.increment_rated_count(year)
                log.info("Album rated: %s - %s (%.1f)", album["artist"], album["title"], rating or 0)

    def _find_duplicate(self, artist, title):
        """Find an existing album with matching artist+title (normalized)."""
        norm_artist = self._normalize_for_match(artist)
        norm_title = self._normalize_for_match(title)
        with db.get_db() as conn:
            row = conn.execute(
                "SELECT * FROM albums WHERE LOWER(artist) = ? AND LOWER(title) = ?",
                (artist.lower(), title.lower())
            ).fetchone()
            if row:
                return row
            candidates = conn.execute(
                "SELECT * FROM albums WHERE LOWER(artist) LIKE ?",
                (f"%{artist.lower()[:15]}%",)
            ).fetchall()
            for c in candidates:
                if self._normalize_for_match(c["title"]) == norm_title:
                    return c
            return None

    def _mark_as_rated(self, album_id, artist, title):
        """Move an album to rated state using RYM cache data."""
        rating = self._get_rym_rating(artist, title)
        db.update_album_state(album_id, "rated")
        with db.get_db() as conn:
            conn.execute(
                "UPDATE albums SET rym_rating = ?, rym_rated_at = ?, updated_at = ? WHERE id = ?",
                (rating, db.now_iso(), db.now_iso(), album_id)
            )
        year = datetime.now(timezone.utc).year
        db.increment_rated_count(year)
        log.info("Auto-rated: %s - %s (%.1f)", artist, title, rating or 0)

    @staticmethod
    def _normalize_for_match(text):
        """Strip punctuation, whitespace, and diacritics for fuzzy matching."""
        import re
        text = text.lower()
        text = re.sub(r'[^\w]', '', text)
        return text

    @staticmethod
    def _strip_release_type(text):
        """Remove trailing release-type suffixes (EP, LP, Single, etc.)."""
        import re
        return re.sub(r'\s*\b(ep|lp|single|deluxe|remaster(?:ed)?|expanded)\s*$', '', text.lower()).strip()

    def _find_in_rym_cache(self, artist, title):
        """Find a matching RYM cache entry using normalized comparison."""
        norm_artist = self._normalize_for_match(artist)
        norm_title = self._normalize_for_match(title)
        norm_title_stripped = self._normalize_for_match(self._strip_release_type(title))
        with db.get_db() as conn:
            rows = conn.execute(
                "SELECT * FROM rym_ratings_cache WHERE LOWER(artist) LIKE ?",
                (f"%{artist.lower()[:15]}%",)
            ).fetchall()
            for row in rows:
                row_norm_title = self._normalize_for_match(row["title"])
                row_norm_artist = self._normalize_for_match(row["artist"])
                if row_norm_artist == norm_artist and row_norm_title == norm_title:
                    return row
                if row_norm_artist == norm_artist and row_norm_title == norm_title_stripped:
                    return row
                if (norm_artist in row_norm_artist and
                        (norm_title in row_norm_title or row_norm_title in norm_title)):
                    return row
            return None

    def _is_rated_on_rym(self, artist, title):
        """Check if album exists in RYM ratings cache."""
        return self._find_in_rym_cache(artist, title) is not None

    def _get_rym_rating(self, artist, title):
        row = self._find_in_rym_cache(artist, title)
        return row["rating"] if row else None

    def check_rym_wishlist(self):
        """Scrape RYM wishlist and add new items to Lidarr."""
        if not self.rym or not self.lidarr or not self._rym_scrape_enabled:
            return
        if not self.config.get("rym", {}).get("wishlist_to_lidarr"):
            return

        log.info("Checking RYM wishlist...")
        wishlist = self.rym.scrape_all_wishlist()
        if not wishlist:
            return

        log.info("Found %d wishlist items", len(wishlist))
        added_count = 0
        for item in wishlist:
            with db.get_db() as conn:
                existing = conn.execute(
                    "SELECT * FROM rym_wishlist_cache WHERE rym_slug = ?",
                    (item["rym_slug"],)
                ).fetchone()
                if existing:
                    continue

                conn.execute(
                    "INSERT INTO rym_wishlist_cache (rym_slug, artist, title, added_to_cache_at) VALUES (?, ?, ?, ?)",
                    (item["rym_slug"], item["artist"], item["title"], db.now_iso())
                )

            if self._is_rated_on_rym(item["artist"], item["title"]):
                continue
            if self.lidarr.is_album_in_library(item["artist"], item["title"]):
                continue

            results = self.lidarr.search_album(f"{item['artist']} {item['title']}")
            if results:
                log.info("Adding wishlist item to Lidarr: %s - %s", item["artist"], item["title"])
                added_count += 1

            with db.get_db() as conn:
                conn.execute(
                    "UPDATE rym_wishlist_cache SET sent_to_lidarr = 1 WHERE rym_slug = ?",
                    (item["rym_slug"],)
                )

        if added_count:
            log.info("Added %d wishlist items to Lidarr", added_count)

    def check_1001_albums(self):
        """Check for today's 1001 Albums Generator album."""
        if not self.gen1001:
            return
        log.info("Checking 1001 Albums Generator...")
        album = self.gen1001.get_current_album()
        if not album:
            return

        search = album_to_search_terms(album)
        nd_album = self.is_in_library(search["artist"], search["title"])

        existing = None
        with db.get_db() as conn:
            existing = conn.execute(
                "SELECT * FROM albums WHERE source = '1001albums' AND title LIKE ?",
                (f"%{search['title'][:30]}%",)
            ).fetchone()

        if existing:
            return

        if not nd_album and self.lidarr:
            results = self.lidarr.search_album(f"{search['artist']} {search['title']}")
            if results:
                log.info("1001 Albums: requesting %s - %s via Lidarr", search["artist"], search["title"])

        db.upsert_album(
            mbid=f"1001-{album.get('uuid', '')}",
            title=search["title"],
            artist=search["artist"],
            year=int(search["year"][:4]) if search.get("year") else None,
            cover_art_url=search.get("cover_url", ""),
            genre_bucket=match_genre_bucket(search.get("genres", []), self.config.get("genre_buckets", [])),
            genre_tags=",".join(search.get("genres", [])),
            state="to-listen",
            source="1001albums",
        )
        log.info("1001 Albums: today's album is %s - %s", search["artist"], search["title"])

    def run_backfill(self, range_="all_time"):
        """Historical backfill from ListenBrainz stats."""
        log.info("Running backfill from ListenBrainz (range=%s)...", range_)
        self.refresh_library_cache()

        offset = 0
        processed = 0
        while True:
            releases = self.lb.get_stats_releases(range_=range_, count=100, offset=offset)
            if not releases:
                break
            for release in releases:
                artist = release.get("artist_name", "")
                title = release.get("release_name", "")
                if not artist or not title:
                    continue

                nd_album = self.is_in_library(artist, title)
                if not nd_album:
                    continue
                if not self.passes_filter(nd_album):
                    continue

                release_mbid = release.get("release_mbid") or nd_album.get("musicBrainzId", "")

                if release_mbid:
                    existing = db.get_album_by_mbid(release_mbid)
                    if existing:
                        continue
                else:
                    release_mbid = f"nd:{nd_album.get('id', '')}"

                dupe = self._find_duplicate(artist, title)
                if dupe:
                    continue

                listen_count = release.get("listen_count", 0)
                track_count = nd_album.get("songCount", 1)
                estimated_completion = min(listen_count / max(track_count, 1), 1.0)
                threshold = self.config.get("album_filter", {}).get("completion_threshold", 0.8)

                if self._is_rated_on_rym(artist, title):
                    state = "rated"
                elif estimated_completion >= threshold:
                    state = "listened-unrated"
                else:
                    state = "listening"

                db.upsert_album(
                    mbid=release_mbid,
                    title=title,
                    artist=artist,
                    track_count=track_count,
                    duration_seconds=nd_album.get("duration", 0),
                    cover_art_url=nd_album.get("coverArt", ""),
                    state=state,
                    completion=estimated_completion,
                    tracks_heard=min(listen_count, track_count),
                    navidrome_id=nd_album.get("id", ""),
                    source="backfill",
                    rym_rating=self._get_rym_rating(artist, title),
                )
                processed += 1

            offset += 100
            if len(releases) < 100:
                break

        log.info("Backfill complete: %d albums processed", processed)

    def backfill_from_navidrome(self):
        """Backfill played albums directly from Navidrome (catches albums without MBIDs)."""
        log.info("Running Navidrome library backfill...")
        self.refresh_library_cache()

        recently_played = self.nd.get_recently_played(size=100)
        processed = 0

        for nd_album in recently_played:
            artist = nd_album.get("artist", "")
            title = nd_album.get("name", "")
            if not artist or not title:
                continue
            if not self.passes_filter(nd_album):
                continue

            play_count = nd_album.get("playCount", 0)
            if play_count == 0:
                continue

            dupe = self._find_duplicate(artist, title)
            if dupe:
                continue

            release_mbid = nd_album.get("musicBrainzId", "")
            if release_mbid:
                existing = db.get_album_by_mbid(release_mbid)
                if existing:
                    continue
            else:
                release_mbid = f"nd:{nd_album.get('id', '')}"

            track_count = nd_album.get("songCount", 1)
            estimated_completion = min(play_count / max(track_count, 1), 1.0)
            threshold = self.config.get("album_filter", {}).get("completion_threshold", 0.8)

            if self._is_rated_on_rym(artist, title):
                state = "rated"
            elif estimated_completion >= threshold:
                state = "listened-unrated"
            else:
                state = "listening"

            db.upsert_album(
                mbid=release_mbid,
                title=title,
                artist=artist,
                track_count=track_count,
                duration_seconds=nd_album.get("duration", 0),
                cover_art_url=nd_album.get("coverArt", ""),
                state=state,
                completion=estimated_completion,
                tracks_heard=min(play_count, track_count),
                navidrome_id=nd_album.get("id", ""),
                source="backfill-navidrome",
                rym_rating=self._get_rym_rating(artist, title),
            )
            processed += 1

        log.info("Navidrome backfill complete: %d albums processed", processed)

    def backfill_genres(self):
        """Re-fetch genres for albums with bucket=Other and no genre_tags.
        Uses artist-level fallback (now built into get_album_metadata)."""
        with db.get_db() as conn:
            albums = conn.execute(
                "SELECT id, mbid, artist, title FROM albums "
                "WHERE genre_bucket = 'Other' AND (genre_tags IS NULL OR genre_tags = '')"
            ).fetchall()

        if not albums:
            log.info("Genre backfill: no albums need genre update")
            return 0

        log.info("Genre backfill: %d albums to check", len(albums))
        updated = 0
        for album in albums:
            mbid = album["mbid"]
            if not mbid or mbid.startswith(("lidarr:", "nd:")):
                continue
            mb_meta = self.mb.get_album_metadata(mbid)
            if not mb_meta:
                continue
            genres = mb_meta.get("genres", [])
            if not genres:
                continue
            bucket = match_genre_bucket(genres, self.config.get("genre_buckets", []))
            with db.get_db() as conn:
                conn.execute(
                    "UPDATE albums SET genre_bucket = ?, genre_tags = ? WHERE id = ?",
                    (bucket, ",".join(genres[:10]), album["id"])
                )
            log.info("Genre backfill: %s - %s → %s (%s)",
                     album["artist"], album["title"], bucket, ", ".join(genres[:3]))
            updated += 1

        log.info("Genre backfill complete: %d/%d albums updated", updated, len(albums))
        return updated

    def check_lidarr_imports(self):
        """Check Lidarr for recently imported albums.

        RYM wishlist items go straight to to-listen.
        Everything else goes to lidarr_pending for user review.
        """
        if not self.lidarr:
            return

        with db.get_db() as conn:
            last_check = conn.execute(
                "SELECT value FROM kv WHERE key = 'lidarr_last_check'"
            ).fetchone()
            since = last_check["value"] if last_check else None

        imports = self.lidarr.get_recent_imports(since_date=since)
        if not imports:
            return

        auto_added = 0
        pending_added = 0
        for item in imports:
            artist = item["artist"]
            title = item["title"]

            dupe = self._find_duplicate(artist, title)
            if dupe:
                continue

            if self._is_rated_on_rym(artist, title):
                continue

            release_mbid = item.get("album_mbid", "")
            if release_mbid:
                existing = db.get_album_by_mbid(release_mbid)
                if existing:
                    continue

            with db.get_db() as conn:
                already_pending = conn.execute(
                    "SELECT 1 FROM lidarr_pending WHERE LOWER(artist) = ? AND LOWER(title) = ?",
                    (artist.lower(), title.lower())
                ).fetchone()
                if already_pending:
                    continue

            nd_album = self.is_in_library(artist, title)
            cover_url = ""
            if nd_album:
                cover_url = nd_album.get("coverArt", "")
                if not self.passes_filter(nd_album):
                    continue

            is_from_wishlist = False
            with db.get_db() as conn:
                wl = conn.execute(
                    "SELECT 1 FROM rym_wishlist_cache WHERE LOWER(artist) LIKE ? AND LOWER(title) LIKE ?",
                    (f"%{artist.lower()[:15]}%", f"%{title.lower()[:15]}%")
                ).fetchone()
                if wl:
                    is_from_wishlist = True

            if is_from_wishlist:
                if not release_mbid:
                    release_mbid = f"lidarr:{artist.lower()[:10]}:{title.lower()[:10]}"
                genre_bucket = "Other"
                is_real_mbid = release_mbid and not release_mbid.startswith(("lidarr:", "nd:"))
                mb_meta = self.mb.get_album_metadata(release_mbid) if is_real_mbid else None
                if mb_meta:
                    genres = mb_meta.get("genres", [])
                    genre_bucket = match_genre_bucket(genres, self.config.get("genre_buckets", []))

                db.upsert_album(
                    mbid=release_mbid,
                    title=title,
                    artist=artist,
                    track_count=nd_album.get("songCount", 0) if nd_album else 0,
                    duration_seconds=nd_album.get("duration", 0) if nd_album else 0,
                    cover_art_url=cover_url,
                    genre_bucket=genre_bucket,
                    state="to-listen",
                    source="wishlist",
                )
                auto_added += 1
                log.info("Wishlist → to-listen: %s - %s", artist, title)
            else:
                with db.get_db() as conn:
                    conn.execute(
                        "INSERT OR IGNORE INTO lidarr_pending (artist, title, album_mbid, cover_url, lidarr_date, added_at) "
                        "VALUES (?, ?, ?, ?, ?, ?)",
                        (artist, title, release_mbid or "", cover_url, item.get("date", ""), db.now_iso())
                    )
                pending_added += 1

        if imports:
            with db.get_db() as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO kv (key, value) VALUES ('lidarr_last_check', ?)",
                    (imports[0]["date"],)
                )

        if auto_added:
            log.info("Added %d wishlist albums to to-listen", auto_added)
        if pending_added:
            log.info("Added %d albums to pending review", pending_added)

    def sync_genre_playlists(self):
        """Sync genre bucket playlists to Navidrome.

        Creates a playlist per genre bucket containing all songs from
        to-listen and listening albums in that bucket.
        """
        from .modules.navidrome import find_album_in_library

        with db.get_db() as conn:
            albums = conn.execute(
                "SELECT id, artist, title, genre_bucket, navidrome_id "
                "FROM albums WHERE state IN ('to-listen', 'listening')"
            ).fetchall()

            playlists_db = conn.execute("SELECT * FROM playlists").fetchall()

        existing_playlists = {p["genre_bucket"]: p for p in playlists_db}
        nd_playlists = self.nd.get_playlists()
        nd_playlist_map = {p.get("name"): p.get("id") for p in nd_playlists}

        buckets = {}
        for album in albums:
            bucket = album["genre_bucket"]
            if bucket not in buckets:
                buckets[bucket] = []
            buckets[bucket].append(album)

        for bucket, bucket_albums in buckets.items():
            playlist_name = f"Earwrym: {bucket}"

            nd_playlist_id = nd_playlist_map.get(playlist_name)
            if not nd_playlist_id:
                nd_playlist_id = self.nd.create_playlist(playlist_name)
                if not nd_playlist_id:
                    log.warning("Failed to create playlist: %s", playlist_name)
                    continue
                log.info("Created Navidrome playlist: %s (%s)", playlist_name, nd_playlist_id)

            with db.get_db() as conn:
                if bucket in existing_playlists:
                    conn.execute(
                        "UPDATE playlists SET navidrome_playlist_id = ? WHERE genre_bucket = ?",
                        (nd_playlist_id, bucket)
                    )
                else:
                    conn.execute(
                        "INSERT INTO playlists (name, navidrome_playlist_id, genre_bucket, created_at) "
                        "VALUES (?, ?, ?, ?)",
                        (playlist_name, nd_playlist_id, bucket, db.now_iso())
                    )

            desired_song_ids = []
            for album in bucket_albums:
                nd_id = album["navidrome_id"]
                if not nd_id:
                    nd_album = find_album_in_library(self.nd, album["artist"], album["title"])
                    if nd_album:
                        nd_id = nd_album.get("id", "")
                        with db.get_db() as conn:
                            conn.execute("UPDATE albums SET navidrome_id = ? WHERE id = ?", (nd_id, album["id"]))
                if nd_id:
                    songs = self.nd.get_album_songs(nd_id)
                    for song in songs:
                        desired_song_ids.append(song.get("id"))

            if desired_song_ids:
                result = self.nd.set_playlist_songs(nd_playlist_id, desired_song_ids)
                count = result.get("songCount", 0) if result else 0
                log.info("Synced %s: %d songs", playlist_name, count)

        log.info("Genre playlist sync complete: %d buckets synced to Navidrome", len(buckets))

        if not self.jellyfin:
            return

        jf_playlists = self.jellyfin.get_playlists()
        jf_playlist_map = {p.get("Name"): p.get("Id") for p in jf_playlists}

        for bucket, bucket_albums in buckets.items():
            playlist_name = f"Earwrym: {bucket}"
            jf_playlist_id = jf_playlist_map.get(playlist_name)

            if not jf_playlist_id:
                jf_playlist_id = self.jellyfin.create_playlist(playlist_name)
                if not jf_playlist_id:
                    log.warning("Failed to create Jellyfin playlist: %s", playlist_name)
                    continue
                log.info("Created Jellyfin playlist: %s", playlist_name)

            self.jellyfin.clear_playlist(jf_playlist_id)

            song_ids = []
            not_found = []
            for album in bucket_albums:
                jf_album = self.jellyfin.search_album(album["artist"], album["title"])
                if jf_album:
                    songs = self.jellyfin.get_album_songs(jf_album["Id"])
                    song_ids.extend(s["Id"] for s in songs)
                else:
                    not_found.append(f"{album['artist']} - {album['title']}")
            if not_found:
                log.debug("Jellyfin %s: %d albums not found: %s",
                          playlist_name, len(not_found), "; ".join(not_found[:5]))

            if song_ids:
                self.jellyfin.add_to_playlist(jf_playlist_id, song_ids)
                log.info("Synced Jellyfin %s: %d songs", playlist_name, len(song_ids))

        log.info("Jellyfin playlist sync complete")
