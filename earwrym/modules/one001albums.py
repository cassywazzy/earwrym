import json
import logging
import math
from urllib.request import Request, urlopen
from urllib.error import HTTPError

log = logging.getLogger(__name__)

BASE_URL = "https://1001albumsgenerator.com/api/v1"
WRITE_URL = "https://1001albumsgenerator.com/api"


class OneThousandOneClient:
    def __init__(self, project_slug, group_slug=None, member_name=None):
        self.slug = project_slug
        self.group_slug = group_slug
        self.member_name = member_name

    def _get(self, url):
        req = Request(url, headers={"User-Agent": "Earwrym/1.0"})
        try:
            with urlopen(req, timeout=15) as resp:
                return json.loads(resp.read())
        except HTTPError as e:
            log.error("1001Albums API error %s: %s", e.code, url)
            return None

    def _post(self, url, payload):
        body = json.dumps(payload).encode()
        req = Request(url, data=body, headers={
            "User-Agent": "Earwrym/1.0",
            "Content-Type": "application/json",
        }, method="POST")
        try:
            with urlopen(req, timeout=15) as resp:
                return json.loads(resp.read())
        except HTTPError as e:
            log.error("1001Albums POST error %s: %s", e.code, url)
            return None

    def get_project(self):
        return self._get(f"{BASE_URL}/projects/{self.slug}")

    def get_group(self):
        if not self.group_slug:
            return None
        return self._get(f"{BASE_URL}/groups/{self.group_slug}")

    def get_current_album(self):
        project = self.get_project()
        if not project:
            return None
        album = project.get("currentAlbum")
        if album:
            album["_generated_album_id"] = project.get("currentAlbumNotes", "")
        return album

    def get_group_current_album(self):
        group = self.get_group()
        if not group:
            return None
        album = group.get("currentAlbum")
        if album:
            album["_group_votes"] = []
            for entry in group.get("history", []):
                if entry.get("album", {}).get("uuid") == album.get("uuid"):
                    album["_group_votes"] = entry.get("votes", [])
        return album

    def get_history(self, limit=20):
        project = self.get_project()
        if not project:
            return []
        history = project.get("history", [])
        return history[:limit]

    def get_group_history(self, limit=20):
        group = self.get_group()
        if not group:
            return []
        return group.get("history", [])[:limit]

    def rate_album(self, album_uuid, rating, notes=""):
        """Rate on personal project. rating: 1-5 integer."""
        url = f"{WRITE_URL}/{self.slug}/{album_uuid}/rate"
        payload = {
            "rating": int(rating),
            "notes": notes,
            "fromHistoryView": False,
        }
        result = self._post(url, payload)
        if result:
            log.info("Rated album %s: %d/5 on personal project", album_uuid, rating)
        return result

    def rate_album_group(self, album_uuid, rating, notes=""):
        """Rate on group project."""
        if not self.group_slug or not self.member_name:
            return None
        url = f"{WRITE_URL}/groups/{self.group_slug}/{self.member_name}/{album_uuid}/rate"
        payload = {
            "rating": int(rating),
            "notes": notes,
        }
        result = self._post(url, payload)
        if result:
            log.info("Rated album %s: %d/5 on group %s", album_uuid, rating, self.group_slug)
        return result

    def get_project_stats(self):
        """Get stats from personal project history."""
        project = self.get_project()
        if not project:
            return {}
        history = project.get("history", [])
        if not history:
            return {"total": 0, "avg_rating": 0, "rated": 0}
        rated = [h for h in history if h.get("rating") and h["rating"] != "did-not-listen"]
        avg = sum(h["rating"] for h in rated) / len(rated) if rated else 0
        return {
            "total": len(history),
            "rated": len(rated),
            "skipped": len(history) - len(rated),
            "avg_rating": round(avg, 2),
        }


def rym_to_1001_rating(rym_rating):
    """Convert RYM scale (0.5-5.0, half stars) to 1001Albums scale (1-5, integers).
    RYM 0.5-1.0 → 1, 1.5-2.0 → 2, 2.5-3.0 → 3, 3.5-4.0 → 4, 4.5-5.0 → 5"""
    if not rym_rating:
        return None
    return max(1, min(5, math.ceil(rym_rating)))


def album_to_search_terms(album_data):
    """Extract artist + title for searching in Lidarr/Navidrome."""
    return {
        "artist": album_data.get("artist", ""),
        "title": album_data.get("name", ""),
        "year": album_data.get("releaseDate", ""),
        "genres": album_data.get("genres", []),
        "sub_genres": album_data.get("subGenres", []),
        "cover_url": (album_data.get("images", [{}])[0].get("url") if album_data.get("images") else None),
        "wikipedia_url": album_data.get("wikipediaUrl"),
        "uuid": album_data.get("uuid"),
        "spotify_id": album_data.get("spotifyId"),
        "tidal_id": album_data.get("tidalId"),
    }
