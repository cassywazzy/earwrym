import time
import logging
from urllib.request import Request, urlopen
from urllib.error import HTTPError
import json

log = logging.getLogger(__name__)

BASE_URL = "https://api.listenbrainz.org/1"


class ListenBrainzClient:
    def __init__(self, username, token=None):
        self.username = username
        self.token = token

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

    def _post(self, path, body):
        if not self.token:
            log.debug("No LB token configured, skipping POST %s", path)
            return None
        url = f"{BASE_URL}{path}"
        data = json.dumps(body).encode("utf-8")
        req = Request(url, data=data, method="POST", headers={
            "User-Agent": "Earwrym/1.0",
            "Authorization": f"Token {self.token}",
            "Content-Type": "application/json",
        })
        try:
            with urlopen(req, timeout=30) as resp:
                return json.loads(resp.read())
        except HTTPError as e:
            log.error("LB API POST error %s: %s", e.code, path)
            return None
        except Exception as e:
            log.warning("LB POST connection error: %s", e)
            return None

    def submit_recording_feedback(self, recording_mbid, score):
        """Submit love (1) / hate (-1) / neutral (0) feedback for a recording."""
        return self._post("/feedback/recording-feedback", {
            "recording_mbid": recording_mbid,
            "score": score,
        })

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


    def get_cf_recommendations(self, count=100, offset=0):
        """Get collaborative filtering recording recommendations."""
        data = self._get(f"/cf/recommendation/user/{self.username}/recording", {
            "count": count, "offset": offset
        })
        if not data:
            return []
        return data.get("payload", {}).get("mbids", [])

    def get_fresh_releases(self, days=30, sort="confidence"):
        """Get personalized fresh releases for the user."""
        data = self._get(f"/user/{self.username}/fresh_releases", {
            "days": days, "sort": sort, "past": "true", "future": "false"
        })
        if not data:
            return []
        return data.get("payload", {}).get("releases", [])

    def get_recording_metadata(self, recording_mbids):
        """Resolve recording MBIDs to full metadata (artist, release, tags)."""
        if not recording_mbids:
            return {}
        url = f"{BASE_URL}/metadata/recording/"
        data = json.dumps({
            "recording_mbids": recording_mbids,
            "inc": "artist tag release"
        }).encode("utf-8")
        req = Request(url, data=data, method="POST", headers={
            "User-Agent": "Earwrym/1.0",
            "Content-Type": "application/json",
        })
        try:
            with urlopen(req, timeout=30) as resp:
                return json.loads(resp.read())
        except Exception as e:
            log.warning("LB metadata lookup failed: %s", e)
            return {}

    def get_similar_artists(self, artist_mbids):
        """Get similar artists from LB Labs session-based algorithm."""
        url = "https://labs.api.listenbrainz.org/similar-artists/json"
        payload = json.dumps([{
            "artist_mbids": artist_mbids,
            "algorithm": "session_based_days_7500_session_300_contribution_5_threshold_10_limit_100_filter_True_skip_30"
        }]).encode("utf-8")
        req = Request(url, data=payload, method="POST", headers={
            "User-Agent": "Earwrym/1.0",
            "Content-Type": "application/json",
        })
        try:
            with urlopen(req, timeout=30) as resp:
                result = json.loads(resp.read())
                if isinstance(result, list) and result:
                    return result[0] if isinstance(result[0], list) else result
                return result
        except Exception as e:
            log.warning("LB similar artists failed: %s", e)
            return []

    def get_playlist_tracks(self, playlist_mbid):
        """Fetch tracks from a specific LB playlist."""
        data = self._get(f"/playlist/{playlist_mbid}", {"fetch_metadata": "true"})
        if not data:
            return []
        tracks = data.get("playlist", {}).get("track", [])
        return tracks


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
