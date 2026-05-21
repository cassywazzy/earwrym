import json
import logging
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

from ..normalize import normalize, tokenize

log = logging.getLogger(__name__)


class QBittorrentClient:
    def __init__(self, url, category="music"):
        self.base_url = url.rstrip("/")
        self.category = category

    def _get(self, endpoint, params=None):
        url = f"{self.base_url}/api/v2/{endpoint}"
        if params:
            from urllib.parse import urlencode
            url += "?" + urlencode(params)
        req = Request(url)
        try:
            with urlopen(req, timeout=10) as resp:
                return json.loads(resp.read())
        except (HTTPError, URLError, OSError) as e:
            log.warning("qBit API error on %s: %s", endpoint, e)
            return None

    def get_music_torrents(self):
        data = self._get("torrents/info", {"filter": "all", "category": self.category})
        if not data:
            return []
        results = []
        for t in data:
            results.append({
                "hash": t.get("hash", ""),
                "name": t.get("name", ""),
                "state": t.get("state", ""),
                "progress": round(t.get("progress", 0) * 100, 1),
                "dlspeed": t.get("dlspeed", 0),
                "seeds": t.get("num_seeds", 0),
                "peers": t.get("num_leechs", 0),
                "size": t.get("total_size", 0),
                "eta": t.get("eta", 0),
                "added_on": t.get("added_on", 0),
            })
        return results

    def match_album(self, artist, title, torrents=None):
        """Find a torrent matching an artist+title pair. Returns best match or None."""
        if torrents is None:
            torrents = self.get_music_torrents()
        if not torrents:
            return None
        norm_artist = normalize(artist)
        norm_title = normalize(title)
        title_tokens = tokenize(title)
        artist_tokens = tokenize(artist)
        best = None
        best_score = 0
        for t in torrents:
            norm_name = normalize(t["name"])
            if norm_artist in norm_name and norm_title in norm_name:
                return t
            artist_strong = norm_artist in norm_name
            if not artist_strong:
                name_tokens = tokenize(t["name"])
                if not artist_tokens.intersection(name_tokens):
                    continue
            matched = sum(1 for tok in title_tokens if tok in norm_name)
            score = matched / len(title_tokens) if title_tokens else 0
            threshold = 0.3 if artist_strong else 0.6
            min_matches = min(2, len(title_tokens))
            if matched >= min_matches and score > best_score and score >= threshold:
                best_score = score
                best = t
        return best
