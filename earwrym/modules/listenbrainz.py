import time
import logging
from urllib.request import Request, urlopen
from urllib.error import HTTPError
import json

log = logging.getLogger(__name__)

BASE_URL = "https://api.listenbrainz.org/1"


class ListenBrainzClient:
    def __init__(self, username):
        self.username = username

    def _get(self, path, params=None):
        url = f"{BASE_URL}{path}"
        if params:
            qs = "&".join(f"{k}={v}" for k, v in params.items() if v is not None)
            url = f"{url}?{qs}"
        req = Request(url, headers={"User-Agent": "Earwrym/1.0"})
        try:
            with urlopen(req, timeout=30) as resp:
                return json.loads(resp.read())
        except HTTPError as e:
            log.error("LB API error %s: %s", e.code, path)
            return None
        except Exception as e:
            log.warning("LB connection error: %s", e)
            return None

    def get_recent_listens(self, count=100, min_ts=None):
        params = {"count": count}
        if min_ts:
            params["min_ts"] = int(min_ts)
        data = self._get(f"/user/{self.username}/listens", params)
        if not data:
            return []
        return data.get("payload", {}).get("listens", [])

    def get_listen_count(self):
        data = self._get(f"/user/{self.username}/listen-count")
        if not data:
            return 0
        return data.get("payload", {}).get("count", 0)

    def get_stats_releases(self, range_="all_time", count=100, offset=0):
        data = self._get(f"/stats/user/{self.username}/releases", {
            "range": range_, "count": count, "offset": offset
        })
        if not data:
            return []
        return data.get("payload", {}).get("releases", [])

    def get_stats_artists(self, range_="all_time", count=25):
        data = self._get(f"/stats/user/{self.username}/artists", {
            "range": range_, "count": count
        })
        if not data:
            return []
        return data.get("payload", {}).get("artists", [])

    def get_stats_listening_activity(self, range_="this_year"):
        data = self._get(f"/stats/user/{self.username}/listening-activity", {
            "range": range_
        })
        if not data:
            return []
        return data.get("payload", {}).get("listening_activity", [])

    def get_recommendation_playlist(self):
        """Get the user's recommended tracks from LB's Troi engine."""
        data = self._get(f"/user/{self.username}/playlists/recommendations")
        if not data:
            return []
        playlists = data.get("payload", {}).get("playlists", [])
        if not playlists:
            return []
        return playlists

    def get_daily_activity(self, range_="this_year"):
        data = self._get(f"/stats/user/{self.username}/daily-activity", {
            "range": range_
        })
        if not data:
            return {}
        return data.get("payload", {}).get("daily_activity", {})


def extract_album_info(listen):
    """Extract album-level info from a LB listen object."""
    track_meta = listen.get("track_metadata", {})
    additional = track_meta.get("additional_info", {})
    mbid_mapping = track_meta.get("mbid_mapping", {})

    return {
        "artist": track_meta.get("artist_name", ""),
        "title": track_meta.get("release_name", ""),
        "track_name": track_meta.get("track_name", ""),
        "release_mbid": mbid_mapping.get("release_mbid") or additional.get("release_mbid", ""),
        "recording_mbid": mbid_mapping.get("recording_mbid") or additional.get("recording_mbid", ""),
        "listened_at": listen.get("listened_at", 0),
    }
