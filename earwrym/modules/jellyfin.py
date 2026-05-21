import json
import logging
from urllib.request import Request, urlopen
from urllib.error import HTTPError
from urllib.parse import urlencode, quote

log = logging.getLogger(__name__)


class JellyfinClient:
    """Jellyfin API client for playlist management."""

    def __init__(self, url, api_key, user_id, music_library_id=None):
        self.base_url = url.rstrip("/")
        self.api_key = api_key
        self.user_id = user_id
        self.music_library_id = music_library_id

    def _request(self, method, path, data=None, params=None):
        url = f"{self.base_url}{path}"
        if params:
            url += "?" + urlencode(params)
        headers = {
            "X-Emby-Token": self.api_key,
            "Content-Type": "application/json",
        }
        body = json.dumps(data).encode() if data else None
        req = Request(url, data=body, headers=headers, method=method)
        try:
            with urlopen(req, timeout=30) as resp:
                content = resp.read()
                if content:
                    return json.loads(content)
                return {}
        except HTTPError as e:
            log.error("Jellyfin %s %s: %s", method, path, e.code)
            return None
        except Exception as e:
            log.warning("Jellyfin connection error: %s", e)
            return None

    def get_playlists(self):
        """Get all playlists for the user."""
        data = self._request("GET", f"/Users/{self.user_id}/Items", params={
            "IncludeItemTypes": "Playlist",
            "Recursive": "true",
        })
        if not data:
            return []
        return data.get("Items", [])

    def create_playlist(self, name, media_type="Audio"):
        """Create a new playlist, return its ID."""
        data = self._request("POST", "/Playlists", data={
            "Name": name,
            "UserId": self.user_id,
            "MediaType": media_type,
        })
        if data:
            return data.get("Id")
        return None

    def get_playlist_items(self, playlist_id):
        """Get items in a playlist."""
        data = self._request("GET", f"/Playlists/{playlist_id}/Items", params={
            "UserId": self.user_id,
        })
        if not data:
            return []
        return data.get("Items", [])

    def add_to_playlist(self, playlist_id, item_ids):
        """Add items to a playlist."""
        if not item_ids:
            return
        ids_str = ",".join(item_ids)
        self._request("POST", f"/Playlists/{playlist_id}/Items", params={
            "Ids": ids_str,
            "UserId": self.user_id,
        })

    def remove_from_playlist(self, playlist_id, entry_ids):
        """Remove items from playlist by their playlist entry IDs."""
        if not entry_ids:
            return
        ids_str = ",".join(entry_ids)
        self._request("DELETE", f"/Playlists/{playlist_id}/Items", params={
            "EntryIds": ids_str,
        })

    def clear_playlist(self, playlist_id):
        """Remove all items from a playlist."""
        items = self.get_playlist_items(playlist_id)
        if items:
            entry_ids = [item.get("PlaylistItemId", item.get("Id")) for item in items]
            self.remove_from_playlist(playlist_id, entry_ids)

    def search_album(self, artist, title):
        """Search for an album in the music library.

        Jellyfin search doesn't handle 'artist album' combined queries well,
        so we search by title only and filter by artist match.
        """
        params = {
            "SearchTerm": title,
            "IncludeItemTypes": "MusicAlbum",
            "Recursive": "true",
            "Limit": 10,
        }
        if self.music_library_id:
            params["ParentId"] = self.music_library_id
        data = self._request("GET", f"/Users/{self.user_id}/Items", params=params)
        items = (data or {}).get("Items", [])

        artist_lower = artist.lower()
        title_lower = title.lower()

        for item in items:
            item_artist = (item.get("AlbumArtist") or "").lower()
            item_title = item.get("Name", "").lower()
            if artist_lower in item_artist and title_lower in item_title:
                return item

        for item in items:
            item_title = item.get("Name", "").lower()
            if title_lower in item_title:
                return item

        if not items:
            params_artist = {
                "SearchTerm": artist,
                "IncludeItemTypes": "MusicAlbum",
                "Recursive": "true",
                "Limit": 20,
            }
            if self.music_library_id:
                params_artist["ParentId"] = self.music_library_id
            data = self._request("GET", f"/Users/{self.user_id}/Items", params=params_artist)
            items = (data or {}).get("Items", [])
            for item in items:
                item_title = item.get("Name", "").lower()
                if title_lower in item_title:
                    return item

        return None

    def get_album_songs(self, album_id):
        """Get all songs in an album."""
        data = self._request("GET", f"/Users/{self.user_id}/Items", params={
            "ParentId": album_id,
            "IncludeItemTypes": "Audio",
            "Recursive": "true",
            "SortBy": "ParentIndexNumber,IndexNumber,SortName",
            "SortOrder": "Ascending",
        })
        if not data:
            return []
        return data.get("Items", [])

    def get_recently_played(self, limit=200):
        """Get recently played tracks grouped by album."""
        data = self._request("GET", f"/Users/{self.user_id}/Items", params={
            "IncludeItemTypes": "Audio",
            "Recursive": "true",
            "SortBy": "DatePlayed",
            "SortOrder": "Descending",
            "Filters": "IsPlayed",
            "Limit": str(limit),
        })
        if not data:
            return []
        albums = {}
        for item in data.get("Items", []):
            album_name = item.get("Album", "")
            artist = item.get("AlbumArtist", "")
            if not album_name or not artist:
                continue
            key = f"{artist.lower()}|{album_name.lower()}"
            played = item.get("UserData", {}).get("LastPlayedDate", "")
            if key not in albums:
                albums[key] = {
                    "artist": artist,
                    "title": album_name,
                    "tracks_played": 0,
                    "last_played": played,
                    "track_names": [],
                }
            albums[key]["tracks_played"] += 1
            albums[key]["track_names"].append(item.get("Name", ""))
            if played > albums[key]["last_played"]:
                albums[key]["last_played"] = played
        return list(albums.values())
