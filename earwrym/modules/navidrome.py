import hashlib
import logging
import random
import string
import time
from urllib.request import Request, urlopen
from urllib.parse import urlencode
from urllib.error import HTTPError
import json
import xml.etree.ElementTree as ET

log = logging.getLogger(__name__)


class NavidromeClient:
    """Subsonic API client for Navidrome."""

    def __init__(self, url, username, password):
        self.base_url = url.rstrip("/")
        self.username = username
        self.password = password

    def _auth_params(self):
        salt = "".join(random.choices(string.ascii_lowercase + string.digits, k=8))
        token = hashlib.md5((self.password + salt).encode()).hexdigest()
        return {
            "u": self.username,
            "t": token,
            "s": salt,
            "v": "1.16.1",
            "c": "earwrym",
            "f": "json",
        }

    def _get(self, endpoint, params=None):
        all_params = self._auth_params()
        if params:
            all_params.update(params)
        url = f"{self.base_url}/rest/{endpoint}?{urlencode(all_params)}"
        req = Request(url, headers={"User-Agent": "Earwrym/1.0"})
        try:
            with urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read())
                return data.get("subsonic-response", {})
        except HTTPError as e:
            log.error("Navidrome API error %s: %s", e.code, endpoint)
            return None

    def get_all_albums(self, size=500, offset=0):
        """Get all albums in library. Paginate with size/offset."""
        resp = self._get("getAlbumList2", {"type": "alphabeticalByName", "size": size, "offset": offset})
        if not resp:
            return []
        album_list = resp.get("albumList2", {}).get("album", [])
        return album_list

    def get_recently_played(self, size=100):
        """Get recently played albums."""
        resp = self._get("getAlbumList2", {"type": "recent", "size": size})
        if not resp:
            return []
        return resp.get("albumList2", {}).get("album", [])

    def get_full_library(self):
        """Fetch entire album library."""
        albums = []
        offset = 0
        while True:
            batch = self.get_all_albums(size=500, offset=offset)
            if not batch:
                break
            albums.extend(batch)
            offset += len(batch)
            if len(batch) < 500:
                break
        return albums

    def get_album(self, album_id):
        """Get album details including track list."""
        resp = self._get("getAlbum", {"id": album_id})
        if not resp:
            return None
        return resp.get("album", {})

    def search(self, query, album_count=5):
        """Search for albums by name."""
        resp = self._get("search3", {"query": query, "albumCount": album_count, "songCount": 0, "artistCount": 0})
        if not resp:
            return []
        return resp.get("searchResult3", {}).get("album", [])

    def get_playlists(self):
        """Get all playlists."""
        resp = self._get("getPlaylists")
        if not resp:
            return []
        return resp.get("playlists", {}).get("playlist", [])

    def create_playlist(self, name):
        """Create a new playlist, return its ID."""
        resp = self._get("createPlaylist", {"name": name})
        if not resp:
            return None
        playlist = resp.get("playlist", {})
        return playlist.get("id")

    def get_playlist(self, playlist_id):
        """Get playlist with tracks."""
        resp = self._get("getPlaylist", {"id": playlist_id})
        if not resp:
            return None
        return resp.get("playlist", {})

    def set_playlist_songs(self, playlist_id, song_ids):
        """Replace all songs in a playlist (Navidrome-compatible)."""
        all_params = self._auth_params()
        all_params["playlistId"] = playlist_id
        base_url = f"{self.base_url}/rest/createPlaylist?{urlencode(all_params)}"
        for sid in song_ids:
            base_url += f"&songId={sid}"
        req = Request(base_url, headers={"User-Agent": "Earwrym/1.0"})
        try:
            with urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read())
                return data.get("subsonic-response", {}).get("playlist", {})
        except HTTPError as e:
            log.error("Set playlist songs error %s", e.code)
            return None

    def add_to_playlist(self, playlist_id, song_ids):
        """Add songs to existing playlist content."""
        current = self.get_playlist(playlist_id)
        existing_ids = []
        if current and current.get("entry"):
            existing_ids = [e.get("id") for e in current["entry"]]
        all_ids = existing_ids + song_ids
        return self.set_playlist_songs(playlist_id, all_ids)

    def remove_from_playlist(self, playlist_id, indices):
        """Remove songs by index, keeping the rest."""
        current = self.get_playlist(playlist_id)
        if not current or not current.get("entry"):
            return
        indices_set = set(indices)
        remaining = [e.get("id") for i, e in enumerate(current["entry"]) if i not in indices_set]
        return self.set_playlist_songs(playlist_id, remaining)

    def get_album_songs(self, album_id):
        """Get all songs for an album."""
        album = self.get_album(album_id)
        if not album:
            return []
        return album.get("song", [])


def find_album_in_library(client, artist, title):
    """Search Navidrome for a specific album. Returns album dict or None."""
    results = client.search(f"{artist} {title}", album_count=10)
    if not results:
        return None
    artist_lower = artist.lower()
    title_lower = title.lower()
    for album in results:
        if (artist_lower in album.get("artist", "").lower() and
                title_lower in album.get("name", "").lower()):
            return album
    for album in results:
        if title_lower in album.get("name", "").lower():
            return album
    return None
