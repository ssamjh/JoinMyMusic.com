import asyncio
import logging
from typing import Optional

import spotipy
from spotipy.oauth2 import SpotifyOAuth
from spotipy.exceptions import SpotifyException
import requests as req_lib

import os

SPOTIFY_CLIENT_ID = os.environ.get("SPOTIFY_CLIENT_ID", "")
SPOTIFY_CLIENT_SECRET = os.environ.get("SPOTIFY_CLIENT_SECRET", "")
SPOTIFY_DEVICE_NAME = os.environ.get("SPOTIFY_DEVICE_NAME", "")
SPOTIFY_REDIRECT_URI = os.environ.get("SPOTIFY_REDIRECT_URI", "http://localhost:8080/api/callback")
TOKEN_CACHE_FILE = os.environ.get("TOKEN_CACHE_FILE", "/data/token_cache.json")

logger = logging.getLogger(__name__)

SCOPE = "user-read-playback-state app-remote-control user-modify-playback-state"

_EMPTY_METADATA = {
    "current": {
        "artist": [],
        "song": "",
        "album": "",
        "songid": "",
        "albumid": "",
        "cover": "",
        "playing": False,
    }
}


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

    async def get_current_playback(self) -> dict:
        try:
            playback = await asyncio.to_thread(self.sp.current_playback)
        except (SpotifyException, req_lib.exceptions.Timeout, req_lib.exceptions.ConnectionError) as e:
            logger.error(f"Error getting playback: {e}")
            return _EMPTY_METADATA

        if not playback:
            return _EMPTY_METADATA

        if SPOTIFY_DEVICE_NAME and playback.get("device", {}).get("name") != SPOTIFY_DEVICE_NAME:
            return _EMPTY_METADATA

        try:
            track = playback["item"]
            album = track["album"]
            artists = [{"name": a["name"], "id": a["id"]} for a in track["artists"]]
            cover = next(
                (img["url"] for img in album["images"] if img.get("height") == 300),
                album["images"][0]["url"] if album["images"] else "",
            )
            return {
                "current": {
                    "artist": artists,
                    "song": track["name"],
                    "album": album["name"],
                    "songid": track["id"],
                    "albumid": album["id"],
                    "cover": cover,
                    "playing": playback["is_playing"],
                }
            }
        except (KeyError, TypeError, AttributeError) as e:
            logger.error(f"Error parsing playback data: {e}")
            return _EMPTY_METADATA

    async def add_to_queue(self, track_id: str) -> None:
        try:
            playback = await asyncio.to_thread(self.sp.current_playback)
        except (SpotifyException, req_lib.exceptions.Timeout) as e:
            raise ValueError(f"Failed to get playback state: {e}")

        if SPOTIFY_DEVICE_NAME and (
            not playback or playback.get("device", {}).get("name") != SPOTIFY_DEVICE_NAME
        ):
            raise ValueError("Music is not playing from the specified device")

        await asyncio.to_thread(self.sp.add_to_queue, uri=f"spotify:track:{track_id}")

    async def skip(self) -> None:
        await asyncio.to_thread(self.sp.next_track)

    async def get_track_info(self, track_id: str) -> dict:
        track = await asyncio.to_thread(self.sp.track, track_id)
        artists = [{"id": a["id"], "name": a["name"]} for a in track["artists"]]
        album = track["album"]
        cover = next(
            (img["url"] for img in album["images"] if img.get("height") == 300),
            album["images"][0]["url"] if album["images"] else "",
        )
        return {
            "song": track["name"],
            "songid": track["id"],
            "artist": artists,
            "album": album["name"],
            "albumid": album["id"],
            "cover": cover,
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
