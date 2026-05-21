"""Multi-source tag/descriptor provider system.

Aggregates music descriptors from multiple sources (MusicBrainz, Last.fm,
Essentia audio analysis, etc.) using a priority-based provider pattern
inspired by komf's metadata aggregation.

Each provider implements fetch_tags(artist, title, mbid) and returns a list of
(tag, tag_type, weight) tuples. The aggregator merges results, deduplicates,
and stores in the album_tags table.
"""

import glob
import json
import logging
import os
import time
from urllib.request import Request, urlopen
from urllib.error import HTTPError
from urllib.parse import quote

from .. import db

log = logging.getLogger(__name__)


class TagProvider:
    """Base class for tag/descriptor providers."""
    name = "base"
    rate_limit = 1.0
    _last_request = 0

    def _throttle(self):
        elapsed = time.time() - self._last_request
        if elapsed < self.rate_limit:
            time.sleep(self.rate_limit - elapsed)
        self._last_request = time.time()

    def _get_json(self, url, headers=None):
        self._throttle()
        hdrs = {"User-Agent": "Earwrym/1.0"}
        if headers:
            hdrs.update(headers)
        req = Request(url, headers=hdrs)
        try:
            with urlopen(req, timeout=15) as resp:
                return json.loads(resp.read())
        except HTTPError as e:
            if e.code == 503:
                time.sleep(2)
                return self._get_json(url, headers)
            log.debug("%s API error %s: %s", self.name, e.code, url)
            return None
        except Exception as e:
            log.debug("%s fetch error: %s", self.name, e)
            return None

    def fetch_tags(self, artist, title, mbid=None):
        """Returns list of (tag_name, tag_type, weight) tuples."""
        raise NotImplementedError


class MusicBrainzTagProvider(TagProvider):
    """Fetch tags from MusicBrainz release-group and artist endpoints.

    MB tags are community-voted and include both genres and descriptors.
    The 'count' field indicates vote confidence.
    MBIDs stored in Earwrym can be release IDs or release-group IDs,
    so we try release-group first, then fall back to release → release-group lookup.
    """
    name = "musicbrainz"
    rate_limit = 1.1

    def _extract_tags(self, data):
        tags = []
        for t in data.get("tags", []):
            if t.get("name") and t.get("count", 0) > 0:
                tags.append((t["name"].lower(), "tag", t["count"]))
        for g in data.get("genres", []):
            if g.get("name") and g.get("count", 0) > 0:
                tags.append((g["name"].lower(), "genre", g["count"]))
        return tags

    def fetch_tags(self, artist, title, mbid=None):
        if not mbid or mbid.startswith(("nd:", "lidarr:", "wishlist:", "1001-")):
            return []

        # Try as release-group first
        rg = self._get_json(
            f"https://musicbrainz.org/ws/2/release-group/{mbid}"
            f"?inc=tags+genres&fmt=json"
        )
        if rg and rg.get("title"):
            tags = self._extract_tags(rg)
            if tags:
                return tags

        # Fall back: treat as release ID, get its release-group
        release = self._get_json(
            f"https://musicbrainz.org/ws/2/release/{mbid}"
            f"?inc=tags+genres+release-groups+artist-credits&fmt=json"
        )
        if not release:
            return []

        tags = self._extract_tags(release)

        rg_data = release.get("release-group", {})
        rg_id = rg_data.get("id")
        if rg_id:
            rg_full = self._get_json(
                f"https://musicbrainz.org/ws/2/release-group/{rg_id}"
                f"?inc=tags+genres&fmt=json"
            )
            if rg_full:
                rg_tags = self._extract_tags(rg_full)
                existing_names = {t[0] for t in tags}
                for t in rg_tags:
                    if t[0] not in existing_names:
                        tags.append(t)

        # Artist-level fallback if still nothing
        if not tags:
            for credit in release.get("artist-credit", []):
                artist_id = credit.get("artist", {}).get("id")
                if not artist_id:
                    continue
                artist_data = self._get_json(
                    f"https://musicbrainz.org/ws/2/artist/{artist_id}"
                    f"?inc=tags+genres&fmt=json"
                )
                if artist_data:
                    tags = self._extract_tags(artist_data)
                    if tags:
                        break

        return tags


class LastFmTagProvider(TagProvider):
    """Fetch tags from Last.fm's album.getTopTags endpoint.

    Last.fm tags are user-generated and often include descriptors like
    "melancholic", "atmospheric", etc. alongside genres.
    Requires an API key but the endpoint is free and generous.
    """
    name = "lastfm"
    rate_limit = 0.25

    def __init__(self, api_key):
        self.api_key = api_key

    def fetch_tags(self, artist, title, mbid=None):
        if not self.api_key:
            return []

        url = (
            f"https://ws.audioscrobbler.com/2.0/"
            f"?method=album.getTopTags"
            f"&artist={quote(artist)}"
            f"&album={quote(title)}"
            f"&api_key={self.api_key}"
            f"&format=json"
        )
        data = self._get_json(url)
        if not data:
            return []

        toptags = data.get("toptags", {}).get("tag", [])
        tags = []
        for t in toptags:
            name = t.get("name", "").lower().strip()
            count = int(t.get("count", 0))
            if name and count > 0:
                tags.append((name, "tag", count))
        return tags


class EssentiaTagProvider(TagProvider):
    """Extract audio features from local music files using Essentia.

    Uses Navidrome's Subsonic API to locate audio files for each album,
    analyzes a representative track, and returns descriptive tags derived
    from BPM, key, danceability, dynamic complexity, etc.
    """
    name = "essentia"
    rate_limit = 2.0

    AUDIO_EXTENSIONS = {".flac", ".mp3", ".ogg", ".opus", ".m4a", ".wav", ".aac", ".wma"}

    def __init__(self, music_dir, navidrome_client=None):
        self.music_dir = music_dir
        self.nd = navidrome_client
        self._es = None

    def _load_essentia(self):
        if self._es is None:
            try:
                import essentia.standard as es
                self._es = es
            except ImportError:
                log.warning("essentia package not installed — audio analysis disabled")
                self._es = False
        return self._es if self._es else None

    @staticmethod
    def _normalize(text):
        from ..normalize import normalize_artist
        return normalize_artist(text)

    def _download_track_from_navidrome(self, artist, title):
        if not self.nd:
            return None
        with db.get_db() as conn:
            album = conn.execute(
                "SELECT navidrome_id FROM albums "
                "WHERE LOWER(artist) = ? AND LOWER(title) = ? AND navidrome_id IS NOT NULL",
                (artist.lower(), title.lower())
            ).fetchone()
        if not album or not album["navidrome_id"]:
            return None
        songs = self.nd.get_album_songs(album["navidrome_id"])
        if not songs:
            return None
        longest = max(songs, key=lambda s: s.get("duration", 0))
        song_id = longest.get("id")
        if not song_id:
            return None
        import tempfile
        from urllib.parse import urlencode
        params = self.nd._auth_params()
        params["id"] = song_id
        url = f"{self.nd.base_url}/rest/download?{urlencode(params)}"
        try:
            from urllib.request import Request as Req, urlopen as uopen
            req = Req(url, headers={"User-Agent": "Earwrym/1.0"})
            with uopen(req, timeout=120) as resp:
                suffix = os.path.splitext(longest.get("path", ".flac"))[1] or ".flac"
                with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
                    tmp.write(resp.read())
                    return tmp.name
        except Exception as e:
            log.debug("Failed to download track for %s - %s: %s", artist, title, e)
            return None

    def _find_file_via_scan(self, artist, title):
        if not os.path.isdir(self.music_dir):
            return None
        artist_norm = self._normalize(artist)
        for entry in os.listdir(self.music_dir):
            entry_path = os.path.join(self.music_dir, entry)
            if not os.path.isdir(entry_path):
                continue
            if self._normalize(entry) != artist_norm:
                continue
            audio_files = [
                os.path.join(entry_path, f) for f in os.listdir(entry_path)
                if os.path.splitext(f)[1].lower() in self.AUDIO_EXTENSIONS
            ]
            if audio_files:
                return max(audio_files, key=os.path.getsize)
        return None

    def _analyze_track(self, filepath):
        es = self._load_essentia()
        if not es:
            return None
        try:
            audio = es.MonoLoader(filename=filepath, sampleRate=44100)()
            features = {}

            bpm, _, _, _, _ = es.RhythmExtractor2013(method="multifeature")(audio)
            features["bpm"] = round(bpm)

            key, scale, strength = es.KeyExtractor()(audio)
            features["key"] = key
            features["scale"] = scale
            features["key_strength"] = round(strength, 2)

            features["danceability"] = round(es.Danceability()(audio)[0], 3)

            dc, _ = es.DynamicComplexity()(audio)
            features["dynamic_complexity"] = round(dc, 2)

            features["energy"] = round(es.Energy()(audio), 2)
            features["loudness"] = round(es.Loudness()(audio), 2)

            duration = len(audio) / 44100.0
            zcr = es.ZeroCrossingRate()(audio)
            features["zero_crossing_rate"] = round(zcr, 4)
            features["duration"] = round(duration, 1)

            return features
        except Exception as e:
            log.debug("Essentia analysis failed for %s: %s", filepath, e)
            return None

    def _features_to_tags(self, features):
        tags = []

        bpm = features.get("bpm", 0)
        if bpm > 0:
            tags.append((f"bpm:{bpm}", "audio_metric", 100))
            if bpm < 80:
                tags.append(("slow-tempo", "audio_feature", 90))
            elif bpm < 110:
                tags.append(("mid-tempo", "audio_feature", 90))
            elif bpm < 140:
                tags.append(("uptempo", "audio_feature", 90))
            else:
                tags.append(("fast-tempo", "audio_feature", 90))

        key = features.get("key")
        scale = features.get("scale")
        if key and scale:
            tags.append((f"key:{key}-{scale}", "audio_metric", 100))
            if scale == "minor":
                tags.append(("minor-key", "audio_feature", 70))
            else:
                tags.append(("major-key", "audio_feature", 70))

        dance = features.get("danceability", 0)
        if dance > 1.5:
            tags.append(("danceable", "audio_feature", 80))
        elif dance < 0.8:
            tags.append(("not-danceable", "audio_feature", 80))

        dc = features.get("dynamic_complexity", 0)
        if dc > 8:
            tags.append(("dynamic", "audio_feature", 75))
        elif dc < 3:
            tags.append(("compressed", "audio_feature", 75))

        zcr = features.get("zero_crossing_rate", 0)
        if zcr > 0.15:
            tags.append(("noisy", "audio_feature", 65))
        elif zcr < 0.03:
            tags.append(("smooth", "audio_feature", 65))

        return tags

    def fetch_tags(self, artist, title, mbid=None):
        tmp_file = None
        try:
            filepath = self._download_track_from_navidrome(artist, title)
            if filepath:
                tmp_file = filepath
            else:
                filepath = self._find_file_via_scan(artist, title)
            if not filepath:
                return []
            features = self._analyze_track(filepath)
            if not features:
                return []
            return self._features_to_tags(features)
        finally:
            if tmp_file and os.path.exists(tmp_file):
                os.unlink(tmp_file)


class TagAggregator:
    """Merges tags from multiple providers and stores in album_tags table.

    Provider priority determines which source's weight wins on conflicts.
    All sources contribute to the tag pool — more sources = richer profile.
    """

    def __init__(self, providers=None):
        self.providers = providers or []

    def enrich_album(self, album_id):
        """Fetch and store tags for a single album from all providers."""
        with db.get_db() as conn:
            album = conn.execute(
                "SELECT id, artist, title, mbid FROM albums WHERE id = ?",
                (album_id,)
            ).fetchone()
            if not album:
                return 0

            existing = conn.execute(
                "SELECT source FROM album_tags WHERE album_id = ? GROUP BY source",
                (album_id,)
            ).fetchall()
            existing_sources = {r["source"] for r in existing}

        total_added = 0
        for provider in self.providers:
            if provider.name in existing_sources:
                continue
            try:
                tags = provider.fetch_tags(
                    album["artist"], album["title"], album["mbid"]
                )
                if tags:
                    ts = db.now_iso()
                    with db.get_db() as conn:
                        for tag_name, tag_type, weight in tags:
                            conn.execute(
                                "INSERT OR IGNORE INTO album_tags "
                                "(album_id, tag, tag_type, source, weight, fetched_at) "
                                "VALUES (?, ?, ?, ?, ?, ?)",
                                (album_id, tag_name, tag_type, provider.name, weight, ts)
                            )
                    total_added += len(tags)
                    log.debug("Tags from %s for %s - %s: %d tags",
                              provider.name, album["artist"], album["title"], len(tags))
            except Exception as e:
                log.warning("Tag provider %s failed for album %d: %s",
                            provider.name, album_id, e)

        return total_added

    def backfill_all(self, batch_size=50):
        """Enrich albums missing tags from any configured provider."""
        provider_names = [p.name for p in self.providers]
        if not provider_names:
            return 0

        placeholders = ",".join("?" for _ in provider_names)
        with db.get_db() as conn:
            albums = conn.execute(
                f"""SELECT DISTINCT a.id FROM albums a
                WHERE a.mbid IS NOT NULL
                AND a.mbid NOT LIKE 'nd:%' AND a.mbid NOT LIKE 'lidarr:%'
                AND a.mbid NOT LIKE 'wishlist:%'
                AND (
                    SELECT COUNT(DISTINCT t.source) FROM album_tags t
                    WHERE t.album_id = a.id AND t.source IN ({placeholders})
                ) < ?
                ORDER BY CASE a.state
                  WHEN 'rated' THEN 0
                  WHEN 'listening' THEN 1
                  WHEN 'listened-unrated' THEN 2
                  WHEN 'to-listen' THEN 3
                  ELSE 4 END
                LIMIT ?""",
                provider_names + [len(provider_names), batch_size]
            ).fetchall()

        if not albums:
            log.info("Tag backfill: all albums enriched by all providers")
            return 0

        log.info("Tag backfill: enriching %d albums (missing ≥1 provider)", len(albums))
        total = 0
        for album in albums:
            added = self.enrich_album(album["id"])
            total += added

        log.info("Tag backfill complete: %d tags added across %d albums",
                 total, len(albums))
        return total
