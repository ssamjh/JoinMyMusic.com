import asyncio
import logging
import re
from typing import Optional

import spotipy
from spotipy.oauth2 import SpotifyOAuth

import os

SPOTIFY_CLIENT_ID = os.environ.get("SPOTIFY_CLIENT_ID", "")
SPOTIFY_CLIENT_SECRET = os.environ.get("SPOTIFY_CLIENT_SECRET", "")
SPOTIFY_REDIRECT_URI = os.environ.get("SPOTIFY_REDIRECT_URI", "http://localhost:8080/callback")
TOKEN_CACHE_FILE = os.environ.get("TOKEN_CACHE_FILE", "/data/token_cache.json")
# Public origin of this API. Cover art is rewritten to point here rather than
# at Spotify's CDN, so nothing the browser loads names Spotify.
API_BASE_URL = os.environ.get("API_BASE_URL", "https://api.joinmymusic.com").rstrip("/")

logger = logging.getLogger(__name__)


# Spotify image ids are hex, but keep the check to "alphanumeric, bounded" so a
# format tweak upstream doesn't break art — it still pins the proxy fetch to a
# single path segment under i.scdn.co, so it can't become an open proxy.
COVER_ID_RE = re.compile(r"^[a-zA-Z0-9]{1,64}$")


def cover_proxy_url(url: Optional[str]) -> Optional[str]:
    """Rewrite an i.scdn.co album-art URL to our own /cover/<id> proxy.

    Spotify's art URLs are content-addressed -- the last path segment is the
    image id and is all the proxy needs. Anything else (empty, already
    rewritten, an unexpected host) is passed through untouched.
    """
    if not url or not url.startswith("https://i.scdn.co/image/"):
        return url
    return f"{API_BASE_URL}/cover/{url.rsplit('/', 1)[-1]}"


SCOPE = "user-read-playback-state app-remote-control user-modify-playback-state"


def album_cover_url(album: dict) -> Optional[str]:
    """Prefer the 300px image; fall back to the first one, then nothing."""
    images = album.get("images") or []
    url = next((img["url"] for img in images if img.get("height") == 300), None)
    if url is None and images:
        url = images[0]["url"]
    return cover_proxy_url(url) or ""


class SpotifyClient:
    def __init__(self):
        self.sp = spotipy.Spotify(
            auth_manager=SpotifyOAuth(
                client_id=SPOTIFY_CLIENT_ID,
                client_secret=SPOTIFY_CLIENT_SECRET,
                redirect_uri=SPOTIFY_REDIRECT_URI,
                scope=SCOPE,
                cache_path=TOKEN_CACHE_FILE,
                open_browser=False,
            ),
            requests_timeout=5,
        )

    async def search(self, query: str) -> list:
        results = await asyncio.to_thread(self.sp.search, q=query, type="track", limit=10)
        tracks = results["tracks"]["items"]
        return [
            {
                "id": t["id"],
                "name": t["name"],
                "artist": t["artists"][0]["name"],
                "album": t["album"]["name"],
                "cover": cover_proxy_url(
                    t["album"]["images"][0]["url"] if t["album"]["images"] else None
                ),
            }
            for t in tracks
        ]

    async def get_track_info(self, track_id: str) -> dict:
        track = await asyncio.to_thread(self.sp.track, track_id)
        artists = [{"id": a["id"], "name": a["name"]} for a in track["artists"]]
        album = track["album"]
        return {
            "song": track["name"],
            "songid": track["id"],
            "artist": artists,
            "album": album["name"],
            "albumid": album["id"],
            "cover": album_cover_url(album),
        }

    def get_auth_url(self) -> str:
        return self.sp.auth_manager.get_authorize_url()

    def handle_callback(self, code: str) -> bool:
        try:
            self.sp.auth_manager.get_access_token(code, as_dict=False)
            return True
        except Exception as e:
            logger.error(f"OAuth callback error: {e}")
            return False

    def is_authenticated(self) -> bool:
        try:
            token_info = self.sp.auth_manager.get_cached_token()
            return bool(token_info and not self.sp.auth_manager.is_token_expired(token_info))
        except Exception:
            return False

    async def refresh_token_if_needed(self):
        try:
            token_info = self.sp.auth_manager.get_cached_token()
            if token_info and "refresh_token" in token_info:
                if self.sp.auth_manager.is_token_expired(token_info):
                    await asyncio.to_thread(
                        self.sp.auth_manager.refresh_access_token,
                        token_info["refresh_token"],
                    )
        except Exception as e:
            logger.error(f"Token refresh error: {e}")
