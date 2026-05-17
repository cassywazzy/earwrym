import json
import logging
import re
import time
from urllib.request import Request, urlopen
from urllib.error import HTTPError
from urllib.parse import quote

log = logging.getLogger(__name__)

MB_BASE = "https://musicbrainz.org/ws/2"
CAA_BASE = "https://coverartarchive.org"
WIKI_API = "https://en.wikipedia.org/api/rest_v1"

_last_mb_request = 0
_mb_delay = 1.1  # MB rate limit: 1 req/sec


class MusicBrainzClient:
    def __init__(self, user_agent="Earwrym/1.0 (https://github.com/earwrym/earwrym)"):
        self.user_agent = user_agent

    def _get_mb(self, path, params=None):
        global _last_mb_request
        elapsed = time.time() - _last_mb_request
        if elapsed < _mb_delay:
            time.sleep(_mb_delay - elapsed)

        url = f"{MB_BASE}{path}"
        if params:
            qs = "&".join(f"{k}={quote(str(v))}" for k, v in params.items())
            url = f"{url}?{qs}&fmt=json"
        else:
            url = f"{url}?fmt=json"

        req = Request(url, headers={"User-Agent": self.user_agent})
        _last_mb_request = time.time()
        try:
            with urlopen(req, timeout=30) as resp:
                return json.loads(resp.read())
        except HTTPError as e:
            if e.code == 503:
                time.sleep(2)
                return self._get_mb(path, params)
            log.error("MB API error %s: %s", e.code, url)
            return None

    def get_release_group(self, mbid):
        """Get release group details including genres/tags."""
        return self._get_mb(f"/release-group/{mbid}", {"inc": "genres+tags+url-rels"})

    def get_release(self, mbid):
        """Get release details including tracks."""
        return self._get_mb(f"/release/{mbid}", {"inc": "recordings+genres+tags+url-rels+release-groups+artist-credits"})

    def get_artist(self, mbid):
        """Get artist details including genres/tags."""
        return self._get_mb(f"/artist/{mbid}", {"inc": "genres+tags"})

    def search_release(self, artist, title):
        """Search for a release by artist + title."""
        query = f'release:"{title}" AND artist:"{artist}"'
        return self._get_mb("/release", {"query": query, "limit": 5})

    def get_album_metadata(self, release_mbid):
        """Get comprehensive album metadata for display. Tries release first, then release-group.
        Falls back to artist-level genres when release/RG have none."""
        release = self.get_release(release_mbid)
        if not release:
            rg = self.get_release_group(release_mbid)
            if rg:
                genres = [t.get("name", "") for t in rg.get("genres", []) + rg.get("tags", []) if t.get("name")]
                if not genres:
                    genres = self._artist_genre_fallback(rg.get("artist-credit", []))
                return {
                    "title": rg.get("title", ""),
                    "artist": rg.get("artist-credit-phrase", ""),
                    "year": (rg.get("first-release-date") or "")[:4],
                    "track_count": 0,
                    "duration_seconds": 0,
                    "genres": genres,
                    "wikipedia_url": None,
                    "release_group_mbid": rg.get("id"),
                }
            return None

        track_count = 0
        duration_ms = 0
        media = release.get("media", [])
        for medium in media:
            tracks = medium.get("tracks", [])
            track_count += len(tracks)
            for track in tracks:
                duration_ms += track.get("length", 0) or 0

        genres = []
        for tag in release.get("genres", []) + release.get("tags", []):
            genres.append(tag.get("name", ""))

        rg = release.get("release-group", {})
        for tag in rg.get("genres", []) + rg.get("tags", []):
            if tag.get("name") not in genres:
                genres.append(tag.get("name", ""))

        if not genres:
            genres = self._artist_genre_fallback(release.get("artist-credit", []))

        wikipedia_url = None
        for rel in rg.get("relations", []) + release.get("relations", []):
            if rel.get("type") == "wikipedia":
                wikipedia_url = rel.get("url", {}).get("resource")
                break

        return {
            "title": release.get("title", ""),
            "artist": release.get("artist-credit-phrase", ""),
            "year": (release.get("date") or "")[:4],
            "track_count": track_count,
            "duration_seconds": duration_ms // 1000,
            "genres": [g for g in genres if g],
            "wikipedia_url": wikipedia_url,
            "release_group_mbid": rg.get("id"),
        }

    def _artist_genre_fallback(self, artist_credits):
        """Fetch genres from the first credited artist when release has none."""
        for credit in artist_credits:
            artist = credit.get("artist", {})
            artist_mbid = artist.get("id")
            if not artist_mbid:
                continue
            artist_data = self.get_artist(artist_mbid)
            if not artist_data:
                continue
            genres = [t.get("name", "") for t in
                      artist_data.get("genres", []) + artist_data.get("tags", [])
                      if t.get("name")]
            if genres:
                log.info("Artist-level genre fallback for %s: %s",
                         artist.get("name", "?"), ", ".join(genres[:5]))
                return genres
        return []


def get_cover_art_url(release_mbid):
    """Get cover art URL from Cover Art Archive using stable /front-500 redirect."""
    if not release_mbid or release_mbid.startswith(("nd:", "lidarr:", "wishlist:")):
        return None
    return f"{CAA_BASE}/release/{release_mbid}/front-500"


def get_wikipedia_blurb(wikipedia_url, max_sentences=2):
    """Fetch first N sentences from a Wikipedia article."""
    if not wikipedia_url:
        return None
    title_match = re.search(r'/wiki/(.+)$', wikipedia_url)
    if not title_match:
        return None
    title = title_match.group(1)
    try:
        url = f"{WIKI_API}/page/summary/{title}"
        req = Request(url, headers={"User-Agent": "Earwrym/1.0"})
        with urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
            extract = data.get("extract", "")
            sentences = extract.split(". ")
            blurb = ". ".join(sentences[:max_sentences])
            if not blurb.endswith("."):
                blurb += "."
            return blurb if len(blurb) > 20 else None
    except (HTTPError, Exception) as e:
        log.debug("Wikipedia fetch failed for %s: %s", title, e)
        return None


GENRE_BUCKET_KEYWORDS = {
    "Metal/Heavy": [
        "metal", "grindcore", "grind", "hardcore", "powerviolence",
        "crust punk", "neocrust", "d-beat", "heavy psych", "hard rock",
        "stoner rock", "sludge", "djent", "nwobhm", "grunge",
        "deathcore", "mathcore", "swancore", "electronicore",
    ],
    "Electronic": [
        "electronic", "electro", "techno", "house", "trance",
        "drum and bass", "dubstep", "breakbeat", "breakcore", "idm",
        "synth-pop", "synthwave", "vaporwave", "uk garage", "2-step",
        "jungle", "footwork", "gabber", "hardstyle", "eurodance",
        "italo", "hi-nrg", "downtempo", "trip hop", "trip-hop",
        "chillout", "witch house", "deconstructed club", "hyperpop",
        "bubblegum bass", "nightcore", "digicore", "dariacore", "edm",
        "big beat", "jersey club", "baltimore club", "juke", "gqom",
        "amapiano", "moombahton", "ebm", "aggrotech", "futurepop",
        "new beat", "nu disco", "complextro", "electroclash",
        "neurofunk", "liquid funk", "colour bass", "midtempo bass",
        "plugg", "digital hardcore", "bassline", "acid house",
        "acid techno", "dub techno", "speed garage", "bass music",
    ],
    "Hip-Hop": [
        "hip hop", "hip-hop", "rap", "trap", "drill", "boom bap",
        "crunk", "g-funk", "chopped and screwed", "cloud rap",
        "dirty south", "hyphy", "bounce", "miami bass", "nerdcore",
        "horrorcore", "grime",
    ],
    "Indie/Alt": [
        "indie", "alternative rock", "alternative pop", "alternative country",
        "britpop", "post-britpop", "post-punk", "emo", "math rock",
        "math pop", "jangle pop", "power pop", "garage rock", "lo-fi",
        "singer-songwriter", "folk rock", "folk punk", "chamber pop",
        "baroque pop", "art rock", "art punk", "psychedelic rock",
        "psychedelic pop", "psychedelic folk", "neo-psychedelia",
        "surf rock", "surf punk", "noise pop", "noise rock", "slowcore",
        "slacker rock", "c86", "twee pop", "madchester", "new wave",
        "acoustic rock", "americana", "progressive rock", "krautrock",
        "crossover prog", "freak folk", "anti-folk", "riot grrrl",
        "queercore", "skate punk", "pop punk",
    ],
    "Dreampop/Shoegaze": [
        "shoegaze", "dream pop", "ethereal wave", "ethereal",
        "dark wave", "darkwave", "coldwave", "post-rock", "space rock",
        "ambient pop", "ambient", "drone", "blackgaze", "doomgaze",
        "new age", "dungeon synth", "psybient", "chillwave",
        "hypnagogic pop", "hauntology", "minimal wave",
    ],
    "Experimental": [
        "experimental", "avant-garde", "avant-prog", "avant-folk",
        "no wave", "industrial", "art pop", "glitch",
        "musique concrète", "electroacoustic", "sound collage",
        "sound art", "plunderphonics", "free improvisation", "free jazz",
        "noise", "microtonal", "minimalism", "post-minimalism",
        "field recording", "acousmatic", "zeuhl", "spectral",
    ],
    "Pop/R&B": [
        "pop", "r&b", "rnb", "soul", "funk", "disco",
        "new jack swing", "city pop", "sophisti-pop", "dance-pop",
        "teen pop", "europop", "k-pop", "j-pop", "c-pop", "latin pop",
        "neo soul", "quiet storm", "motown", "northern soul", "doo-wop",
        "gospel", "reggae", "reggaeton", "ska", "bossa nova", "mpb",
        "dancehall",
    ],
}


def match_genre_bucket(genres, buckets_config=None):
    """Match genre tags to best bucket using substring matching."""
    if not genres:
        return "Other"
    genre_lower = [g.lower() for g in genres]
    best_bucket = "Other"
    best_score = 0
    for bucket_name, keywords in GENRE_BUCKET_KEYWORDS.items():
        score = 0
        for g in genre_lower:
            for kw in keywords:
                if kw in g:
                    score += 1
                    break
        if score > best_score:
            best_score = score
            best_bucket = bucket_name
    return best_bucket
