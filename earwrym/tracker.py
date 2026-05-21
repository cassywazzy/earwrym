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
from .modules.qbittorrent import QBittorrentClient
from .modules.jellyfin import JellyfinClient
from .modules.one001albums import OneThousandOneClient, album_to_search_terms
from .modules.tag_providers import TagAggregator, MusicBrainzTagProvider, LastFmTagProvider, EssentiaTagProvider
from .modules.taste import compute_profile
from .normalize import normalize, normalize_artist, music_match

log = logging.getLogger(__name__)


class EarwrymTracker:
    def __init__(self, config):
        self.config = config
        self.lb = ListenBrainzClient(
            config["listenbrainz"]["username"],
            token=config.get("listenbrainz", {}).get("token"),
        )
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
            ota = config["one_thousand_one_albums"]
            self.gen1001 = OneThousandOneClient(
                ota["project_slug"],
                group_slug=ota.get("group_slug"),
                member_name=ota.get("member_name"),
            )
        else:
            self.gen1001 = None

        if config.get("qbittorrent", {}).get("enabled"):
            self.qbt = QBittorrentClient(
                config["qbittorrent"]["url"],
                category=config["qbittorrent"].get("category", "music"),
            )
        else:
            self.qbt = None

        if config.get("jellyfin", {}).get("enabled"):
            self.jellyfin = JellyfinClient(
                config["jellyfin"]["url"],
                config["jellyfin"]["api_key"],
                config["jellyfin"]["user_id"],
                config["jellyfin"].get("music_library_id"),
            )
        else:
            self.jellyfin = None

        providers = [MusicBrainzTagProvider()]
        lastfm_key = config.get("lastfm", {}).get("api_key")
        if lastfm_key:
            providers.append(LastFmTagProvider(lastfm_key))
        essentia_dir = config.get("essentia", {}).get("music_dir")
        if essentia_dir:
            providers.append(EssentiaTagProvider(essentia_dir, navidrome_client=self.nd))
        self.tag_aggregator = TagAggregator(providers)

        self._library_cache = {}
        self._library_cache_time = 0
        self._last_lb_ts = 0

    def refresh_library_cache(self, force=False):
        """Refresh the Navidrome library cache."""
        now = time.time()
        if not force and now - self._library_cache_time < 300:
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
        """Check if album exists in Navidrome library (two-pass normalization)."""
        self.refresh_library_cache()
        key = f"{artist.lower()}|{title.lower()}"
        if key in self._library_cache:
            return self._library_cache[key]
        na = normalize_artist(artist)
        nt = normalize(title)
        for cache_key, album in self._library_cache.items():
            parts = cache_key.split("|", 1)
            if len(parts) != 2:
                continue
            c_artist, c_title = parts
            ca = normalize_artist(c_artist)
            if na not in ca and ca not in na:
                continue
            ct = normalize(c_title)
            if ct == nt:
                return album
            if ct.startswith(nt) or nt.startswith(ct):
                return album
        nt_agg = normalize(title, aggressive=True)
        for cache_key, album in self._library_cache.items():
            parts = cache_key.split("|", 1)
            if len(parts) != 2:
                continue
            c_artist, c_title = parts
            ca = normalize_artist(c_artist)
            if na not in ca and ca not in na:
                continue
            if normalize(c_title, aggressive=True) == nt_agg:
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
                existing = self._find_duplicate(info["artist"], info["title"], navidrome_id=nd_album.get("id"))
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

    _SYNTHETIC_PREFIXES = ("nd:", "1001-", "wishlist:", "lidarr:", "disco:", "oracle:")

    def _create_album(self, release_mbid, listen_info, nd_album):
        """Create a new album entry with metadata."""
        dupe = self._find_duplicate(listen_info["artist"], listen_info["title"])
        if dupe:
            return dupe["id"]

        is_real_mbid = release_mbid and not release_mbid.startswith(self._SYNTHETIC_PREFIXES)
        mb_meta = self.mb.get_album_metadata(release_mbid) if is_real_mbid else None
        genres = []
        wikipedia_url = None
        track_count = nd_album.get("songCount", 0)
        duration = nd_album.get("duration", 0)
        rg_mbid = None

        if mb_meta:
            genres = mb_meta.get("genres", [])
            wikipedia_url = mb_meta.get("wikipedia_url")
            rg_mbid = mb_meta.get("release_group_mbid")
            if mb_meta.get("track_count"):
                track_count = mb_meta["track_count"]
            if mb_meta.get("duration_seconds"):
                duration = mb_meta["duration_seconds"]

        cover_url = f"/api/cover/{nd_album['id']}" if nd_album.get("id") else ""
        if not cover_url and is_real_mbid:
            cover_url = get_cover_art_url(release_mbid, rg_mbid) or ""

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

    def poll_jellyfin_listens(self):
        """Poll Jellyfin for recently played albums and record listens."""
        if not self.jellyfin:
            return
        played = self.jellyfin.get_recently_played(limit=200)
        if not played:
            return
        recorded = 0
        for entry in played:
            nd_album = self.is_in_library(entry["artist"], entry["title"])
            if not nd_album:
                continue
            if not self.passes_filter(nd_album):
                continue

            existing = self._find_duplicate(entry["artist"], entry["title"], navidrome_id=nd_album.get("id"))
            if existing and existing["state"] in ("rated", "dismissed"):
                continue

            if not existing:
                release_mbid = nd_album.get("musicBrainzId", "") or f"nd:{nd_album.get('id', '')}"
                existing = db.get_album_by_mbid(release_mbid)

            if not existing:
                if self._is_rated_on_rym(entry["artist"], entry["title"]):
                    continue
                release_mbid = nd_album.get("musicBrainzId", "") or f"nd:{nd_album.get('id', '')}"
                album_id = self._create_album(release_mbid, {
                    "title": entry["title"],
                    "artist": entry["artist"],
                    "release_mbid": nd_album.get("musicBrainzId", ""),
                    "track_name": "",
                    "listened_at": 0,
                }, nd_album)
                if not album_id:
                    continue
            else:
                album_id = existing["id"]
                if existing["state"] == "to-listen":
                    with db.get_db() as conn:
                        conn.execute("UPDATE albums SET state = 'listening' WHERE id = ?", (album_id,))

            played_dt = entry["last_played"][:10] if entry["last_played"] else ""
            for track_name in entry["track_names"]:
                ts_str = f"{played_dt}T00:00:00Z" if played_dt else ""
                db.record_listen(album_id, track_name, ts_str)

            self._update_completion(album_id, nd_album)
            recorded += 1

        if recorded:
            log.info("Jellyfin listen sync: %d albums updated", recorded)

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

    def _find_duplicate(self, artist, title, navidrome_id=None):
        """Find an existing album with matching artist+title (two-pass normalization) or navidrome_id."""
        if navidrome_id:
            with db.get_db() as conn:
                row = conn.execute(
                    "SELECT * FROM albums WHERE navidrome_id = ?", (navidrome_id,)
                ).fetchone()
                if row:
                    return row
        na = normalize_artist(artist)
        nt = normalize(title)
        with db.get_db() as conn:
            row = conn.execute(
                "SELECT * FROM albums WHERE LOWER(artist) = ? AND LOWER(title) = ?",
                (artist.lower(), title.lower())
            ).fetchone()
            if row:
                return row
            candidates = conn.execute(
                "SELECT * FROM albums WHERE state NOT IN ('dismissed')"
            ).fetchall()
            for c in candidates:
                ca = normalize_artist(c["artist"])
                if (ca in na or na in ca) and normalize(c["title"]) == nt:
                    return c
            nt_agg = normalize(title, aggressive=True)
            for c in candidates:
                ca = normalize_artist(c["artist"])
                if (ca in na or na in ca) and normalize(c["title"], aggressive=True) == nt_agg:
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
        self._push_lb_feedback(album_id, rating)

    def _push_lb_feedback(self, album_id, rating):
        """Push love/hate feedback to ListenBrainz based on RYM rating.
        3.0+ = love, <2.0 = hate, 2.0-2.9 = skip."""
        if not self.lb.token or not rating:
            return
        score = 1 if rating >= 3.0 else (-1 if rating < 2.0 else 0)
        if score == 0:
            return
        with db.get_db() as conn:
            album = conn.execute("SELECT mbid FROM albums WHERE id = ?", (album_id,)).fetchone()
        if not album or not album["mbid"] or album["mbid"].startswith(("nd:", "wishlist:", "lidarr:")):
            return
        mb_meta = self.mb.get_release(album["mbid"])
        if not mb_meta:
            return
        recording_mbids = []
        for medium in mb_meta.get("media", []):
            for track in medium.get("tracks", []):
                rec = track.get("recording", {})
                if rec.get("id"):
                    recording_mbids.append(rec["id"])
        pushed = 0
        for rec_mbid in recording_mbids:
            result = self.lb.submit_recording_feedback(rec_mbid, score)
            if result:
                pushed += 1
        if pushed:
            label = "love" if score == 1 else "hate"
            log.info("LB feedback: %s %d recordings for album %d", label, pushed, album_id)

    def _find_in_rym_cache(self, artist, title):
        """Find a matching RYM cache entry using two-pass normalization."""
        na = normalize_artist(artist)
        nt = normalize(title)
        nt_agg = normalize(title, aggressive=True)
        with db.get_db() as conn:
            rows = conn.execute(
                "SELECT * FROM rym_ratings_cache WHERE LOWER(artist) LIKE ?",
                (f"%{artist.lower()[:15]}%",)
            ).fetchall()
            for row in rows:
                ra = normalize_artist(row["artist"])
                rt = normalize(row["title"])
                if ra == na and rt == nt:
                    return row
                rt_agg = normalize(row["title"], aggressive=True)
                if ra == na and rt_agg == nt_agg:
                    return row
                if (na in ra or ra in na) and (nt in rt or rt in nt):
                    return row
            return None

    def _is_rated_on_rym(self, artist, title):
        """Check if album exists in RYM ratings cache."""
        return self._find_in_rym_cache(artist, title) is not None

    def _get_rym_rating(self, artist, title):
        row = self._find_in_rym_cache(artist, title)
        return row["rating"] if row else None

    def backfill_wikipedia_blurbs(self, batch_size=20):
        """Fetch Wikipedia blurbs for albums that have real MBIDs but no blurb."""
        with db.get_db() as conn:
            albums = conn.execute(
                "SELECT id, mbid, artist, title FROM albums "
                "WHERE (wikipedia_blurb IS NULL OR wikipedia_blurb = '') "
                "AND mbid NOT LIKE 'nd:%' AND mbid NOT LIKE 'wishlist:%' "
                "AND mbid NOT LIKE 'lidarr:%' AND mbid NOT LIKE '1001-%' "
                "AND mbid NOT LIKE 'oracle:%' "
                "ORDER BY CASE state WHEN 'rated' THEN 0 WHEN 'listening' THEN 1 "
                "WHEN 'listened-unrated' THEN 2 ELSE 3 END "
                "LIMIT ?",
                (batch_size,)
            ).fetchall()
        if not albums:
            log.info("Wikipedia blurb backfill: all albums covered")
            return 0
        filled = 0
        for album in albums:
            mb_meta = self.mb.get_album_metadata(album["mbid"])
            if not mb_meta or not mb_meta.get("wikipedia_url"):
                continue
            blurb = get_wikipedia_blurb(mb_meta["wikipedia_url"])
            if blurb:
                with db.get_db() as conn:
                    conn.execute(
                        "UPDATE albums SET wikipedia_blurb = ?, updated_at = ? WHERE id = ?",
                        (blurb, db.now_iso(), album["id"])
                    )
                filled += 1
                log.debug("Blurb added: %s - %s", album["artist"], album["title"])
        log.info("Wikipedia blurb backfill: %d/%d albums filled", filled, len(albums))
        return filled

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
            if self._find_duplicate(item["artist"], item["title"]):
                continue
            if self.is_in_library(item["artist"], item["title"]):
                continue
            if self.lidarr.is_album_in_library(item["artist"], item["title"]):
                continue

            if self._request_via_lidarr(item["artist"], item["title"], source="Wishlist"):
                added_count += 1

            with db.get_db() as conn:
                conn.execute(
                    "UPDATE rym_wishlist_cache SET sent_to_lidarr = 1 WHERE rym_slug = ?",
                    (item["rym_slug"],)
                )

        if added_count:
            log.info("Added %d wishlist items to Lidarr", added_count)

    def _request_via_lidarr(self, artist_name, album_title, source=""):
        """Add artist to Lidarr and monitor+search the specific album by title match."""
        if not self.lidarr:
            return False

        norm_target = normalize(album_title)
        artist_id = None

        na = normalize_artist(artist_name)
        artists = self.lidarr.get_all_artists()
        for a in artists:
            if normalize_artist(a.get("artistName", "")) == na:
                artist_id = a["id"]
                break
        if not artist_id:
            for a in artists:
                a_norm = normalize_artist(a.get("artistName", ""))
                if na in a_norm or a_norm in na:
                    artist_id = a["id"]
                    break

        if not artist_id:
            artist_results = self.lidarr.search_artist(artist_name)
            if not artist_results:
                log.info("%s: artist not found in Lidarr search: %s", source, artist_name)
                return False
            artist_data = artist_results[0]
            artist_data["qualityProfileId"] = self.lidarr.quality_profile_id
            artist_data["metadataProfileId"] = artist_data.get("metadataProfileId") or 1
            artist_data["rootFolderPath"] = self.lidarr.root_folder
            artist_data["monitored"] = True
            artist_data["monitorNewItems"] = "none"
            artist_data["addOptions"] = {"monitor": "existing", "searchForMissingAlbums": False}
            result = self.lidarr._request("POST", "artist", artist_data)
            if not result:
                log.warning("%s: failed to add artist %s to Lidarr", source, artist_name)
                return False
            artist_id = result["id"]
            log.info("%s: added artist %s to Lidarr", source, artist_name)

        matched = None
        for attempt in range(3):
            albums = self.lidarr._request("GET", f"album?artistId={artist_id}") or []
            for a in albums:
                if normalize(a.get("title", "")) == norm_target:
                    matched = a
                    break
            if not matched:
                agg_target = normalize(album_title, aggressive=True)
                for a in albums:
                    agg_a = normalize(a.get("title", ""), aggressive=True)
                    if agg_target == agg_a or norm_target in normalize(a.get("title", "")) or normalize(a.get("title", "")) in norm_target:
                        matched = a
                        break
            if matched or attempt == 2:
                break
            time.sleep(3)

        if not matched:
            log.info("%s: album '%s' not found in %s's Lidarr discography (%d albums checked)", source, album_title, artist_name, len(albums))
            return False

        matched["monitored"] = True
        self.lidarr._request("PUT", f"album/{matched['id']}", matched)
        self.lidarr._request("POST", "command", {"name": "AlbumSearch", "albumIds": [matched["id"]]})
        log.info("%s: requested %s - %s via Lidarr (album id %d)", source, artist_name, album_title, matched["id"])
        return True

    def sync_navidrome_links(self):
        """Link Earwrym albums to Navidrome and fill missing metadata."""
        self.refresh_library_cache(force=True)
        with db.get_db() as conn:
            candidates = conn.execute(
                "SELECT id, artist, title, state, navidrome_id, track_count, cover_art_url FROM albums "
                "WHERE state IN ('to-listen', 'listening')"
            ).fetchall()
        if not candidates:
            return
        linked = 0
        enriched = 0
        for album in candidates:
            nd_album = self.is_in_library(album["artist"], album["title"])
            if not nd_album:
                continue

            nd_id = nd_album.get("id", "")
            updates = {}
            if not album["navidrome_id"]:
                updates["navidrome_id"] = nd_id
                updates["state"] = "listening" if album["state"] == "to-listen" else album["state"]
                linked += 1
            if not album["track_count"] and nd_album.get("songCount"):
                updates["track_count"] = nd_album["songCount"]
            if nd_id and (not album["cover_art_url"] or not album["cover_art_url"].startswith("/api/cover/")):
                updates["cover_art_url"] = f"/api/cover/{nd_id}"
            if updates:
                sets = ", ".join(f"{k} = ?" for k in updates)
                vals = list(updates.values()) + [album["id"]]
                with db.get_db() as conn:
                    conn.execute(f"UPDATE albums SET {sets} WHERE id = ?", vals)
                if not album["navidrome_id"] and "navidrome_id" in updates:
                    log.info("Linked to Navidrome: %s - %s (nd:%s)", album["artist"], album["title"], nd_id)
                elif updates:
                    enriched += 1
        if linked or enriched:
            log.info("Navidrome sync: %d linked, %d enriched", linked, enriched)

    def discover_navidrome_albums(self):
        """Find albums on Navidrome not yet tracked in Earwrym and add them."""
        self.refresh_library_cache(force=True)
        if not self._library_cache:
            return

        with db.get_db() as conn:
            tracked_nd = {r["navidrome_id"] for r in conn.execute(
                "SELECT navidrome_id FROM albums WHERE navidrome_id IS NOT NULL AND navidrome_id != ''"
            ).fetchall()}
            tracked_titles = {(r["artist"].lower(), normalize(r["title"]))
                             for r in conn.execute("SELECT artist, title FROM albums").fetchall()}

        min_tracks = self.config.get("album_filter", {}).get("min_tracks", 4)
        min_dur = self.config.get("album_filter", {}).get("min_duration_seconds", 480)
        added = 0
        for nd_album in self._library_cache.values():
            nd_id = nd_album.get("id", "")
            if nd_id in tracked_nd:
                continue
            artist = nd_album.get("artist", "")
            title = nd_album.get("name", "")
            if not artist or not title:
                continue
            norm_key = (artist.lower(), normalize(title))
            if norm_key in tracked_titles:
                continue
            if nd_album.get("songCount", 0) < min_tracks:
                continue
            if nd_album.get("duration", 0) < min_dur:
                continue
            if self._is_rated_on_rym(artist, title):
                continue

            release_mbid = nd_album.get("musicBrainzId", "") or f"nd:{nd_id}"
            if release_mbid and release_mbid != f"nd:{nd_id}":
                existing = db.get_album_by_mbid(release_mbid)
                if existing:
                    with db.get_db() as conn:
                        conn.execute(
                            "UPDATE albums SET navidrome_id = ? WHERE id = ? AND (navidrome_id IS NULL OR navidrome_id = '')",
                            (nd_id, existing["id"])
                        )
                    continue

            is_real = release_mbid and not release_mbid.startswith(self._SYNTHETIC_PREFIXES)
            mb_meta = self.mb.get_album_metadata(release_mbid) if is_real else None
            genres = mb_meta.get("genres", []) if mb_meta else []
            if not genres:
                genres = self.mb.get_artist_genres_by_name(artist)
            bucket = match_genre_bucket(genres, self.config.get("genre_buckets", []))

            db.upsert_album(
                mbid=release_mbid,
                title=title, artist=artist,
                year=nd_album.get("year"),
                track_count=nd_album.get("songCount", 0),
                duration_seconds=nd_album.get("duration", 0),
                cover_art_url=f"/api/cover/{nd_id}",
                genre_bucket=bucket,
                genre_tags=",".join(genres[:10]),
                state="to-listen",
                navidrome_id=nd_id,
                source="navidrome",
            )
            added += 1
            tracked_nd.add(nd_id)
            tracked_titles.add(norm_key)
        if added:
            log.info("Navidrome discovery: %d new albums added", added)

    def handle_lidarr_event(self, event_type, artist_name, album_title, album_mbid=""):
        """Process a real-time Lidarr webhook event for a single album."""
        if event_type == "Grab":
            log.info("Lidarr grab (webhook): %s - %s", artist_name, album_title)
            return

        if event_type in ("DownloadFailure", "ImportFailure"):
            log.warning("Lidarr %s (webhook): %s - %s", event_type, artist_name, album_title)
            return

        if event_type not in ("Download", "AlbumImport"):
            return

        log.info("Lidarr import (webhook): %s - %s", artist_name, album_title)
        self.refresh_library_cache(force=True)

        dupe = self._find_duplicate(artist_name, album_title)
        if dupe:
            nd_album = self.is_in_library(artist_name, album_title)
            if nd_album:
                updates = {}
                if not dupe["navidrome_id"]:
                    updates["navidrome_id"] = nd_album.get("id", "")
                if dupe["state"] == "to-listen":
                    updates["state"] = "listening"
                if not dupe.get("track_count") and nd_album.get("songCount"):
                    updates["track_count"] = nd_album["songCount"]
                if not dupe.get("cover_art_url") and nd_album.get("id"):
                    updates["cover_art_url"] = f"/api/cover/{nd_album['id']}"
                if updates:
                    sets = ", ".join(f"{k} = ?" for k in updates)
                    vals = list(updates.values()) + [dupe["id"]]
                    with db.get_db() as conn:
                        conn.execute(f"UPDATE albums SET {sets} WHERE id = ?", vals)
                    log.info("Webhook linked album %d: %s", dupe["id"], list(updates.keys()))
            return

        if self._is_rated_on_rym(artist_name, album_title):
            return

        if album_mbid:
            existing = db.get_album_by_mbid(album_mbid)
            if existing:
                return

        nd_album = self.is_in_library(artist_name, album_title)
        if nd_album and not self.passes_filter(nd_album):
            return

        is_from_wishlist = False
        with db.get_db() as conn:
            wl = conn.execute(
                "SELECT 1 FROM rym_wishlist_cache WHERE LOWER(artist) LIKE ? AND LOWER(title) LIKE ?",
                (f"%{artist_name.lower()[:15]}%", f"%{album_title.lower()[:15]}%")
            ).fetchone()
            if wl:
                is_from_wishlist = True

        if is_from_wishlist:
            release_mbid = album_mbid or f"lidarr:{artist_name.lower()[:10]}:{album_title.lower()[:10]}"
            genre_bucket = "Other"
            is_real_mbid = release_mbid and not release_mbid.startswith(self._SYNTHETIC_PREFIXES)
            mb_meta = self.mb.get_album_metadata(release_mbid) if is_real_mbid else None
            if mb_meta:
                genres = mb_meta.get("genres", [])
                genre_bucket = match_genre_bucket(genres, self.config.get("genre_buckets", []))
            cover_url = nd_album.get("coverArt", "") if nd_album else ""
            db.upsert_album(
                mbid=release_mbid, title=album_title, artist=artist_name,
                track_count=nd_album.get("songCount", 0) if nd_album else 0,
                duration_seconds=nd_album.get("duration", 0) if nd_album else 0,
                cover_art_url=cover_url, genre_bucket=genre_bucket,
                state="to-listen", source="wishlist",
            )
            log.info("Webhook: wishlist → to-listen: %s - %s", artist_name, album_title)
        else:
            cover_url = nd_album.get("coverArt", "") if nd_album else ""
            with db.get_db() as conn:
                already = conn.execute(
                    "SELECT 1 FROM lidarr_pending WHERE LOWER(artist) = ? AND LOWER(title) = ?",
                    (artist_name.lower(), album_title.lower())
                ).fetchone()
                if not already:
                    conn.execute(
                        "INSERT OR IGNORE INTO lidarr_pending (artist, title, album_mbid, cover_url, lidarr_date, added_at) "
                        "VALUES (?, ?, ?, ?, ?, ?)",
                        (artist_name, album_title, album_mbid or "", cover_url, db.now_iso(), db.now_iso())
                    )
                    log.info("Webhook: %s - %s → pending", artist_name, album_title)

    def retry_stale_lidarr_grabs(self):
        """Blocklist stuck downloads and re-request missing albums via Lidarr."""
        if not self.lidarr:
            return
        with db.get_db() as conn:
            stale = conn.execute(
                "SELECT id, artist, title FROM albums "
                "WHERE (navidrome_id IS NULL OR navidrome_id = '') "
                "AND state IN ('to-listen', 'listening') "
                "ORDER BY id DESC LIMIT 50"
            ).fetchall()
        if not stale:
            return

        blocklisted = 0
        retried = 0
        requested = 0
        queue = self.lidarr.get_queue()
        all_artists = self.lidarr.get_all_artists()

        for album in stale:
            did_blocklist = self._blocklist_stuck_queue_items(album, queue)
            if did_blocklist:
                blocklisted += 1
                continue

            norm_target = normalize(album["title"])
            artist_id = None
            na = normalize_artist(album["artist"])
            for a in all_artists:
                if na in normalize_artist(a.get("artistName", "")) or normalize_artist(a.get("artistName", "")) in na:
                    artist_id = a["id"]
                    break
            if artist_id:
                lidarr_albums = self.lidarr._request("GET", f"album?artistId={artist_id}") or []
                found = False
                for la in lidarr_albums:
                    la_norm = normalize(la.get("title", ""))
                    if la_norm == norm_target or norm_target in la_norm or la_norm in norm_target:
                        found = True
                        stats = la.get("statistics", {})
                        if la["monitored"] and stats.get("trackFileCount", 0) == 0:
                            in_queue = any(
                                qi.get("album_mbid") == la.get("foreignAlbumId") for qi in queue
                            )
                            if not in_queue:
                                self.lidarr._request("POST", "command", {"name": "AlbumSearch", "albumIds": [la["id"]]})
                                log.info("Retry stale grab: %s - %s (Lidarr album %d)", album["artist"], album["title"], la["id"])
                                retried += 1
                        break
                if found:
                    continue
            if self._request_via_lidarr(album["artist"], album["title"], source="Auto-request"):
                requested += 1
        if blocklisted or retried or requested:
            log.info("Stale grab retry: %d blocklisted, %d re-searched, %d newly requested", blocklisted, retried, requested)

    def _blocklist_stuck_queue_items(self, album, queue):
        """Blocklist queue items with fatal download errors. Returns True if anything was blocklisted.

        Only blocklists truly broken downloads (magnet can't resolve, no files, invalid torrent).
        Import failures and slow downloads are left alone — Lidarr handles stalled detection natively.
        """
        FATAL_ERRORS = ("cannot resolve magnet", "no files found", "not a valid torrent")
        for qi in queue:
            err = qi.get("error", "")
            if not err or not any(fe in err.lower() for fe in FATAL_ERRORS):
                continue
            norm_db = normalize(album["title"])
            norm_qi = normalize(qi["title"])
            if normalize_artist(album["artist"]) in normalize_artist(qi.get("artist", "")) and \
               (norm_db == norm_qi or norm_db in norm_qi or norm_qi in norm_db):
                qid = qi.get("id")
                if not qid:
                    continue
                from urllib.request import Request, urlopen
                from urllib.error import HTTPError
                url = f"{self.lidarr.base_url}/api/v1/queue/{qid}?removeFromClient=true&blocklist=true"
                req = Request(url, headers={"X-Api-Key": self.lidarr.api_key}, method="DELETE")
                try:
                    urlopen(req, timeout=30)
                    log.info("Blocklisted stuck queue item: %s - %s (error: %s)", qi["artist"], qi["title"], err[:60])
                except HTTPError as e:
                    log.warning("Failed to blocklist queue %s: %s", qid, e.code)
                return True
        return False

    def check_1001_albums(self):
        """Check for today's 1001 Albums Generator album and auto-request via Lidarr."""
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

        auto_request = self.config.get("one_thousand_one_albums", {}).get("auto_request", True)
        if not nd_album and self.lidarr and auto_request:
            self._request_via_lidarr(search["artist"], search["title"], source="1001 Albums")

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
            genres = []
            if mbid and not any(mbid.startswith(p) for p in self._SYNTHETIC_PREFIXES):
                mb_meta = self.mb.get_album_metadata(mbid)
                if mb_meta:
                    genres = mb_meta.get("genres", [])
            if not genres:
                genres = self.mb.get_artist_genres_by_name(album["artist"])
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

    def backfill_cover_art(self):
        """Fix albums with missing or broken cover art URLs.
        Tries: CAA release → CAA release-group → Navidrome proxy."""
        from urllib.request import Request as Req, urlopen as uopen

        with db.get_db() as conn:
            albums = conn.execute(
                "SELECT id, mbid, artist, title, cover_art_url, navidrome_id FROM albums"
            ).fetchall()

        fixed = 0
        for album in albums:
            url = album["cover_art_url"]
            needs_fix = False

            if not url:
                needs_fix = True
            elif "coverartarchive.org" in url:
                try:
                    req = Req(url, method="HEAD", headers={"User-Agent": "Earwrym/1.0"})
                    uopen(req, timeout=8)
                except Exception:
                    needs_fix = True

            if not needs_fix:
                continue

            mbid = album["mbid"]
            rg_mbid = None
            if mbid and not mbid.startswith(("lidarr:", "nd:", "wishlist:", "1001-")):
                mb_meta = self.mb.get_album_metadata(mbid)
                if mb_meta:
                    rg_mbid = mb_meta.get("release_group_mbid")

            new_url = get_cover_art_url(mbid, rg_mbid) if mbid else None
            if not new_url and album["navidrome_id"]:
                new_url = f"/api/cover/{album['navidrome_id']}"

            if new_url and new_url != url:
                with db.get_db() as conn:
                    conn.execute("UPDATE albums SET cover_art_url = ? WHERE id = ?",
                                 (new_url, album["id"]))
                log.info("Cover art fix: %s - %s → %s",
                         album["artist"], album["title"], new_url[:60])
                fixed += 1

        log.info("Cover art backfill: %d albums fixed", fixed)
        return fixed

    def check_lidarr_imports(self):
        """Check Lidarr for recently imported albums.

        RYM wishlist items go straight to to-listen.
        Everything else goes to lidarr_pending for user review.
        Also links existing unlinked albums when new imports arrive.
        """
        if not self.lidarr:
            return

        self.refresh_library_cache(force=True)

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

        if imports:
            self.sync_navidrome_links()

    def sync_genre_playlists(self):
        """Sync genre bucket playlists to Navidrome.

        Creates a playlist per genre bucket containing all songs from
        to-listen and listening albums in that bucket.
        """
        from .modules.navidrome import find_album_in_library

        with db.get_db() as conn:
            albums = conn.execute(
                "SELECT id, artist, title, genre_bucket, navidrome_id "
                "FROM albums WHERE state IN ('to-listen', 'listening') "
                "AND source != 'navidrome'"
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

    def sync_1001_playlist(self):
        """Sync a '1001 Albums' playlist to Navidrome and Jellyfin."""
        from .modules.navidrome import find_album_in_library

        with db.get_db() as conn:
            albums = conn.execute(
                "SELECT id, artist, title, navidrome_id FROM albums WHERE source = '1001albums' ORDER BY added_at"
            ).fetchall()

        if not albums:
            return

        playlist_name = "Earwrym: 1001 Albums"
        nd_playlists = self.nd.get_playlists()
        nd_playlist_map = {p.get("name"): p.get("id") for p in nd_playlists}
        nd_playlist_id = nd_playlist_map.get(playlist_name)

        if not nd_playlist_id:
            nd_playlist_id = self.nd.create_playlist(playlist_name)
            if not nd_playlist_id:
                log.warning("Failed to create 1001 playlist in Navidrome")
            else:
                log.info("Created Navidrome playlist: %s", playlist_name)

        if nd_playlist_id:
            song_ids = []
            for album in albums:
                nd_id = album["navidrome_id"]
                if not nd_id:
                    nd_album = find_album_in_library(self.nd, album["artist"], album["title"])
                    if nd_album:
                        nd_id = nd_album.get("id", "")
                        with db.get_db() as conn:
                            conn.execute("UPDATE albums SET navidrome_id = ? WHERE id = ?",
                                         (nd_id, album["id"]))
                if nd_id:
                    songs = self.nd.get_album_songs(nd_id)
                    song_ids.extend(s.get("id") for s in songs)
            if song_ids:
                self.nd.set_playlist_songs(nd_playlist_id, song_ids)
                log.info("Synced %s: %d songs", playlist_name, len(song_ids))

        if self.jellyfin:
            jf_playlists = self.jellyfin.get_playlists()
            jf_playlist_map = {p.get("Name"): p.get("Id") for p in jf_playlists}
            jf_playlist_id = jf_playlist_map.get(playlist_name)

            if not jf_playlist_id:
                jf_playlist_id = self.jellyfin.create_playlist(playlist_name)
                if not jf_playlist_id:
                    log.warning("Failed to create 1001 playlist in Jellyfin")
                else:
                    log.info("Created Jellyfin playlist: %s", playlist_name)

            if jf_playlist_id:
                self.jellyfin.clear_playlist(jf_playlist_id)
                song_ids = []
                for album in albums:
                    jf_album = self.jellyfin.search_album(album["artist"], album["title"])
                    if jf_album:
                        songs = self.jellyfin.get_album_songs(jf_album["Id"])
                        song_ids.extend(s["Id"] for s in songs)
                if song_ids:
                    self.jellyfin.add_to_playlist(jf_playlist_id, song_ids)
                    log.info("Synced Jellyfin %s: %d songs", playlist_name, len(song_ids))

    # --- Recommendation Playlists + Auto-Download ---

    def sync_recommendation_playlist(self, limit=30):
        """Sync top recommendations to Navidrome + Jellyfin playlists."""
        from .modules.taste import get_recommendations
        from .modules.navidrome import find_album_in_library

        ranked = get_recommendations(limit=limit)
        if not ranked:
            return {"status": "no recommendations", "tracks": 0}

        playlist_name = "Earwrym: Recommendations"
        nd_playlists = self.nd.get_playlists()
        nd_playlist_map = {p.get("name"): p.get("id") for p in nd_playlists}
        nd_playlist_id = nd_playlist_map.get(playlist_name)

        if not nd_playlist_id:
            nd_playlist_id = self.nd.create_playlist(playlist_name)
            if not nd_playlist_id:
                log.warning("Failed to create recommendations playlist")
                return {"status": "error", "tracks": 0}

        song_ids = []
        with db.get_db() as conn:
            for r in ranked:
                album = conn.execute(
                    "SELECT artist, title, navidrome_id FROM albums WHERE id = ?",
                    (r["album_id"],)
                ).fetchone()
                if not album:
                    continue
                nd_id = album["navidrome_id"]
                if not nd_id:
                    nd_album = find_album_in_library(self.nd, album["artist"], album["title"])
                    if nd_album:
                        nd_id = nd_album.get("id", "")
                        conn.execute("UPDATE albums SET navidrome_id = ? WHERE id = ?",
                                     (nd_id, r["album_id"]))
                if nd_id:
                    songs = self.nd.get_album_songs(nd_id)
                    for s in songs:
                        song_ids.append(s.get("id"))

        if song_ids:
            self.nd.set_playlist_songs(nd_playlist_id, song_ids)
            log.info("Synced %s: %d songs from %d albums", playlist_name, len(song_ids), len(ranked))

        if self.jellyfin:
            jf_playlists = self.jellyfin.get_playlists()
            jf_map = {p.get("Name"): p.get("Id") for p in jf_playlists}
            jf_id = jf_map.get(playlist_name)
            if not jf_id:
                jf_id = self.jellyfin.create_playlist(playlist_name)
            if jf_id:
                self.jellyfin.clear_playlist(jf_id)
                jf_song_ids = []
                with db.get_db() as conn:
                    for r in ranked:
                        album = conn.execute(
                            "SELECT artist, title FROM albums WHERE id = ?", (r["album_id"],)
                        ).fetchone()
                        if album:
                            jf_album = self.jellyfin.search_album(album["artist"], album["title"])
                            if jf_album:
                                songs = self.jellyfin.get_album_songs(jf_album["Id"])
                                jf_song_ids.extend(s["Id"] for s in songs)
                if jf_song_ids:
                    self.jellyfin.add_to_playlist(jf_id, jf_song_ids)
                    log.info("Synced Jellyfin %s: %d songs", playlist_name, len(jf_song_ids))

        return {"status": "synced", "tracks": len(song_ids), "albums": len(ranked)}

    def auto_download_recommendations(self, count=5, min_score=0.55):
        """Auto-request top recommended albums via Lidarr if not in local library."""
        from .modules.taste import get_recommendations

        ranked = get_recommendations(limit=count * 3)
        requested = 0
        skipped = 0

        with db.get_db() as conn:
            for r in ranked:
                if requested >= count:
                    break
                if r["final_score"] < min_score:
                    continue

                album = conn.execute(
                    "SELECT artist, title, navidrome_id FROM albums WHERE id = ?",
                    (r["album_id"],)
                ).fetchone()
                if not album or album["navidrome_id"]:
                    skipped += 1
                    continue

                success = self._request_via_lidarr(
                    album["artist"], album["title"], source="auto-rec"
                )
                if success:
                    requested += 1
                    log.info("Auto-rec: requested %s - %s (score %.2f)",
                             album["artist"], album["title"], r["final_score"])
                else:
                    skipped += 1

        log.info("Auto-download recs: %d requested, %d skipped", requested, skipped)
        return {"requested": requested, "skipped": skipped}

    # --- Discography Tracker ---

    DISCOGRAPHY_TYPES = ["album", "ep", "live"]

    def get_discography_artists(self):
        """Get all tracked artists with completion stats."""
        with db.get_db() as conn:
            return conn.execute(
                "SELECT * FROM discography_artists ORDER BY pinned DESC, name"
            ).fetchall()

    def get_discography_suggestions(self):
        """Find artists with 2+ albums in the DB that aren't tracked yet."""
        with db.get_db() as conn:
            tracked = {r["artist_mbid"] for r in
                       conn.execute("SELECT artist_mbid FROM discography_artists").fetchall()
                       if r["artist_mbid"]}
            artists = conn.execute(
                "SELECT artist, COUNT(*) as cnt FROM albums "
                "WHERE state IN ('rated', 'listening', 'listened-unrated') "
                "GROUP BY artist HAVING cnt >= 2 ORDER BY cnt DESC"
            ).fetchall()
        suggestions = []
        for a in artists:
            name = a["artist"]
            if any(name.lower() == t.lower() for t in
                   [r["name"] for r in self.get_discography_artists()]):
                continue
            suggestions.append({"name": name, "album_count": a["cnt"]})
        return suggestions[:20]

    def add_discography_artist(self, artist_name, artist_mbid=None):
        """Add an artist to the discography tracker and fetch their releases."""
        if not artist_mbid:
            results = self.mb.search_artist(artist_name)
            if results and results.get("artists"):
                for a in results["artists"]:
                    if a.get("name", "").lower() == artist_name.lower():
                        artist_mbid = a["id"]
                        break
                if not artist_mbid:
                    artist_mbid = results["artists"][0]["id"]
                    artist_name = results["artists"][0].get("name", artist_name)

        if not artist_mbid:
            log.warning("Could not find artist MBID for %s", artist_name)
            return None

        ts = db.now_iso()
        with db.get_db() as conn:
            existing = conn.execute(
                "SELECT id FROM discography_artists WHERE artist_mbid = ?",
                (artist_mbid,)
            ).fetchone()
            if existing:
                return existing["id"]
            conn.execute(
                "INSERT INTO discography_artists (name, artist_mbid, pinned, added_at, updated_at) "
                "VALUES (?, ?, 1, ?, ?)",
                (artist_name, artist_mbid, ts, ts)
            )
            artist_id = conn.execute(
                "SELECT id FROM discography_artists WHERE artist_mbid = ?",
                (artist_mbid,)
            ).fetchone()["id"]

        self._refresh_discography(artist_id, artist_mbid)
        return artist_id

    def _refresh_discography(self, artist_id, artist_mbid):
        """Fetch release groups from MB and cross-reference with local albums + RYM ratings."""
        rgs = self.mb.get_artist_release_groups(
            artist_mbid, primary_types=self.DISCOGRAPHY_TYPES
        )
        if not rgs:
            return

        ts = db.now_iso()
        with db.get_db() as conn:
            conn.execute("DELETE FROM discography_releases WHERE artist_id = ?", (artist_id,))
            artist_row = conn.execute(
                "SELECT name FROM discography_artists WHERE id = ?", (artist_id,)
            ).fetchone()
            artist_name = artist_row["name"] if artist_row else ""

            local_albums = conn.execute(
                "SELECT id, mbid, title, state, rym_rating FROM albums WHERE LOWER(artist) = ?",
                (artist_name.lower(),)
            ).fetchall()

            rym_ratings = conn.execute(
                "SELECT title, rating FROM rym_ratings_cache WHERE LOWER(artist) = ?",
                (artist_name.lower(),)
            ).fetchall()

            for rg in rgs:
                rg_mbid = rg.get("id")
                rg_title = rg.get("title", "")
                rg_type = rg.get("primary-type", "Album").lower()
                rg_year = None
                fd = rg.get("first-release-date", "")
                if fd and len(fd) >= 4:
                    try:
                        rg_year = int(fd[:4])
                    except ValueError:
                        pass

                status = "missing"
                linked_album_id = None
                rym_rating = None
                norm_rg = normalize(rg_title)
                norm_rg_agg = normalize(rg_title, aggressive=True)

                for la in local_albums:
                    norm_la = normalize(la["title"])
                    if norm_la == norm_rg or norm_la in norm_rg or norm_rg in norm_la or \
                       normalize(la["title"], aggressive=True) == norm_rg_agg:
                        linked_album_id = la["id"]
                        rym_rating = la["rym_rating"]
                        if la["state"] == "rated":
                            status = "rated"
                        elif la["state"] in ("listening", "listened-unrated"):
                            status = "listened"
                        elif la["state"] == "to-listen":
                            status = "queued"
                        break

                if status == "missing":
                    for rr in rym_ratings:
                        norm_rr = normalize(rr["title"])
                        if norm_rr == norm_rg or norm_rr in norm_rg or norm_rg in norm_rr:
                            status = "rated"
                            rym_rating = rr["rating"]
                            break

                conn.execute(
                    "INSERT INTO discography_releases "
                    "(artist_id, title, release_group_mbid, release_type, year, status, album_id, rym_rating, added_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(artist_id, release_group_mbid) DO UPDATE SET "
                    "status = excluded.status, album_id = excluded.album_id, year = excluded.year, "
                    "rym_rating = excluded.rym_rating",
                    (artist_id, rg_title, rg_mbid, rg_type, rg_year, status, linked_album_id, rym_rating, ts)
                )

            total = conn.execute(
                "SELECT COUNT(*) FROM discography_releases WHERE artist_id = ?",
                (artist_id,)
            ).fetchone()[0]
            completed = conn.execute(
                "SELECT COUNT(*) FROM discography_releases "
                "WHERE artist_id = ? AND status IN ('rated', 'listened')",
                (artist_id,)
            ).fetchone()[0]
            conn.execute(
                "UPDATE discography_artists SET total_releases = ?, completed_releases = ?, updated_at = ? "
                "WHERE id = ?",
                (total, completed, ts, artist_id)
            )

        log.info("Discography refresh: %s — %d releases, %d completed",
                 artist_name, total, completed)

    def get_discography_detail(self, artist_id):
        """Get all releases for an artist, with linked album data."""
        with db.get_db() as conn:
            artist = conn.execute(
                "SELECT * FROM discography_artists WHERE id = ?", (artist_id,)
            ).fetchone()
            releases = conn.execute(
                "SELECT dr.*, a.navidrome_id, "
                "COALESCE(dr.rym_rating, a.rym_rating) AS rym_rating "
                "FROM discography_releases dr "
                "LEFT JOIN albums a ON dr.album_id = a.id "
                "WHERE dr.artist_id = ? ORDER BY dr.year, dr.title",
                (artist_id,)
            ).fetchall()
        return artist, releases

    def refresh_all_discographies(self):
        """Refresh release lists and statuses for all tracked artists."""
        artists = self.get_discography_artists()
        for a in artists:
            if a["artist_mbid"]:
                self._refresh_discography(a["id"], a["artist_mbid"])
        log.info("Refreshed %d discographies", len(artists))
