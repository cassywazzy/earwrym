import json
import logging
from urllib.request import Request, urlopen
from urllib.error import HTTPError

log = logging.getLogger(__name__)


class LidarrClient:
    def __init__(self, url, api_key, quality_profile_id=1, root_folder="/music"):
        self.base_url = url.rstrip("/")
        self.api_key = api_key
        self.quality_profile_id = quality_profile_id
        self.root_folder = root_folder

    def _request(self, method, endpoint, data=None):
        url = f"{self.base_url}/api/v1/{endpoint}"
        headers = {
            "X-Api-Key": self.api_key,
            "Content-Type": "application/json",
        }
        body = json.dumps(data).encode() if data else None
        req = Request(url, data=body, headers=headers, method=method)
        try:
            with urlopen(req, timeout=30) as resp:
                return json.loads(resp.read())
        except HTTPError as e:
            body_text = e.read().decode() if e.fp else ""
            log.error("Lidarr API error %s on %s %s: %s", e.code, method, endpoint, body_text[:200])
            return None

    def get_all_artists(self):
        return self._request("GET", "artist") or []

    def get_all_albums(self):
        return self._request("GET", "album") or []

    def search_artist(self, name):
        from urllib.parse import quote
        return self._request("GET", f"artist/lookup?term={quote(name)}") or []

    def search_album(self, term):
        from urllib.parse import quote
        return self._request("GET", f"album/lookup?term={quote(term)}") or []

    def add_artist_with_album(self, artist_data, album_mbid):
        """Add artist to Lidarr with a specific album monitored."""
        artist_data["qualityProfileId"] = self.quality_profile_id
        artist_data["metadataProfileId"] = artist_data.get("metadataProfileId") or 1
        artist_data["rootFolderPath"] = self.root_folder
        artist_data["monitored"] = True
        artist_data["monitorNewItems"] = "none"
        artist_data["addOptions"] = {
            "monitor": "none",
            "searchForMissingAlbums": False,
        }
        result = self._request("POST", "artist", artist_data)
        if not result:
            return None

        albums = self._request("GET", f"album?artistId={result['id']}") or []
        for album in albums:
            fg = album.get("foreignAlbumId", "")
            if fg == album_mbid:
                album["monitored"] = True
                self._request("PUT", f"album/{album['id']}", album)
                self._request("POST", "command", {
                    "name": "AlbumSearch",
                    "albumIds": [album["id"]]
                })
                break
        return result

    def is_album_in_library(self, artist_name, album_title):
        """Check if an album already exists in Lidarr (by name match)."""
        artists = self.get_all_artists()
        for artist in artists:
            if artist_name.lower() in artist.get("artistName", "").lower():
                albums = self._request("GET", f"album?artistId={artist['id']}") or []
                for album in albums:
                    if album_title.lower() in album.get("title", "").lower():
                        return True
        return False

    def is_artist_in_library(self, artist_name):
        """Check if artist exists in Lidarr."""
        artists = self.get_all_artists()
        return any(artist_name.lower() in a.get("artistName", "").lower() for a in artists)

    def get_queue(self):
        data = self._request("GET", "queue?pageSize=100&includeAlbum=true&includeArtist=true")
        if not data:
            return []
        results = []
        for record in data.get("records", []):
            album_info = record.get("album", {})
            artist_info = record.get("artist", {})
            size = record.get("size", 0)
            sizeleft = record.get("sizeleft", 0)
            pct = round((1 - sizeleft / size) * 100, 1) if size > 0 else 0
            results.append({
                "id": record.get("id"),
                "artist": artist_info.get("artistName", ""),
                "title": album_info.get("title", ""),
                "album_mbid": album_info.get("foreignAlbumId", ""),
                "status": record.get("trackedDownloadState", record.get("status", "")),
                "tracked_status": record.get("trackedDownloadStatus", ""),
                "error": record.get("errorMessage", ""),
                "progress": pct,
                "timeleft": record.get("timeleft", ""),
                "added": record.get("added", ""),
            })
        return results

    def get_recent_imports(self, since_date=None):
        """Get albums recently imported (downloaded) via Lidarr history."""
        data = self._request("GET", "history?pageSize=200&sortKey=date&sortDirection=descending&includeAlbum=true&includeArtist=true")
        if not data:
            return []
        records = data.get("records", [])
        results = []
        seen = set()
        for record in records:
            if record.get("eventType") not in ("trackFileImported", "downloadFolderImported", "albumImportIncomplete"):
                continue
            album_info = record.get("album", {})
            artist_info = record.get("artist", {})
            title = album_info.get("title", "")
            artist = artist_info.get("artistName", "")
            album_mbid = album_info.get("foreignAlbumId", "")
            if not title or not artist:
                continue
            key = f"{artist.lower()}|{title.lower()}"
            if key in seen:
                continue
            seen.add(key)
            if since_date and record.get("date", "") < since_date:
                break
            results.append({
                "artist": artist,
                "title": title,
                "album_mbid": album_mbid,
                "date": record.get("date", ""),
            })
        return results
