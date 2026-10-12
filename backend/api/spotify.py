import asyncio
import logging
import re
from typing import Optional
from urllib.parse import urlsplit

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
# Spotify image ids are 40 hex characters; the "ab67" prefix marks an image
# asset. Anything else (placeholders, playlist art, a bare URL fragment) is
# not treated as art.
IMAGE_ID_RE = re.compile(r"^ab67[0-9a-f]{36}$")
SPOTIFY_IMAGE_URI_RE = re.compile(r"^spotify:image:([0-9a-f]{40})$")
# The 300px album size, which the rest of the app prefers.
PREFERRED_COVER_PREFIX = "ab67616d00001e02"


def cover_image_id(value: object) -> Optional[str]:
    """Return the Spotify image id from a spotify:image URI or i.scdn.co image URL.

    Returns None for anything that is not a recognisable Spotify image id, so
    callers can treat it as missing art and look it up elsewhere.
    """
    if not isinstance(value, str):
        return None
    value = value.strip()
    match = SPOTIFY_IMAGE_URI_RE.match(value)
    if match:
        candidate = match.group(1)
    else:
        parts = urlsplit(value)
        if parts.scheme != "https" or parts.netloc != "i.scdn.co" or not parts.path.startswith("/image/"):
            return None
        candidate = parts.path[len("/image/"):]
    return candidate if IMAGE_ID_RE.match(candidate) else None


def cover_url_for_id(image_id: str) -> str:
    return f"{API_BASE_URL}/cover/{image_id}"


def best_cover_id(values) -> Optional[str]:
    """Choose one image id from several candidate values, preferring the 300px album size."""
    ids = [image_id for image_id in (cover_image_id(value) for value in values) if image_id]
    return next((i for i in ids if i.startswith(PREFERRED_COVER_PREFIX)), ids[0] if ids else None)


def cover_proxy_url(url: Optional[str]) -> Optional[str]:
    """Return our own /cover/<id> proxy URL for Spotify album art, else None.

    Spotify's art is content-addressed, so the image id is all the proxy needs.
    Unrecognised values are None rather than passed through, so the browser never
    requests Spotify's placeholder or a non-image URL.
    """
    image_id = cover_image_id(url)
    return cover_url_for_id(image_id) if image_id else None


SCOPE = "user-read-playback-state app-remote-control user-modify-playback-state"


def album_cover_url(album: dict) -> Optional[str]:
    """Prefer the 300px image among the recognised art ids; then nothing."""
    images = album.get("images") or [] if isinstance(album, dict) else []
    usable = [img for img in images if isinstance(img, dict) and cover_image_id(img.get("url"))]
    usable.sort(key=lambda img: img.get("height") != 300)
    image_id = best_cover_id(img.get("url") for img in usable)
    return cover_url_for_id(image_id) if image_id else ""


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
