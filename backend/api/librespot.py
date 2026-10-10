import asyncio
import logging
import os
import re
import time
from collections import OrderedDict
from typing import Optional

import httpx

from spotify import SpotifyClient, cover_proxy_url

# go-librespot control API of the audio container (unauthenticated; reachable on
# the Compose network, published on the host's loopback only).
LIBRESPOT_API_URL = os.environ.get("LIBRESPOT_API_URL", "http://audio:3678").rstrip("/")

# Artist/album ids come from the Web API, which go-librespot does not report.
# One lookup per track is cached; failed lookups are retried after this many seconds.
TRACK_CACHE_SIZE = 50
TRACK_RETRY_SECS = 60

logger = logging.getLogger(__name__)

# go-librespot reports release_date as e.g. "year:2023 month:7 day:3".
YEAR_RE = re.compile(r"\d{4}")


def _empty_metadata() -> dict:
    return {
        "current": {
            "artist": [],
            "song": "",
            "album": "",
            "songid": "",
            "albumid": "",
            "cover": "",
            "year": "",
            "playing": False,
            "duration_ms": 0,
            "progress_ms": 0,
        }
    }


def _year(release_date: Optional[str]) -> str:
    match = YEAR_RE.search(release_date or "")
    return match.group(0) if match else ""


class LibrespotClient:
    """Now-playing and playback control through the go-librespot REST API."""

    def __init__(self, spotify: SpotifyClient, base_url: str = LIBRESPOT_API_URL):
        self._spotify = spotify
        self._http = httpx.AsyncClient(base_url=base_url, timeout=httpx.Timeout(2.0))
        self._down = False  # tracks reachability so an outage logs once, not every poll
        # track id -> {"ok": bool, "data": {...}, "retry_at": monotonic seconds}
        self._track_cache: OrderedDict[str, dict] = OrderedDict()
        self._inflight: dict[str, asyncio.Task] = {}

    def _note_reachable(self, reachable: bool, detail: str = "") -> None:
        if not reachable and not self._down:
            logger.error(f"go-librespot unreachable at {self._http.base_url}: {detail}")
            self._down = True
        elif reachable and self._down:
            logger.info("go-librespot reachable again")
            self._down = False

    async def get_status(self) -> Optional[dict]:
        """Return /status as a dict, or None when there is no session or an error."""
        try:
            resp = await self._http.get("/status")
        except httpx.HTTPError as e:
            self._note_reachable(False, str(e))
            return None
        self._note_reachable(True)
        if resp.status_code == 204:  # no active session
            return None
        if resp.status_code != 200:
            logger.error(f"go-librespot /status returned HTTP {resp.status_code}")
            return None
        try:
            return resp.json()
        except ValueError as e:
            logger.error(f"go-librespot /status returned invalid JSON: {e}")
            return None

    async def _track_extras(self, track_id: str) -> dict:
        """Artist/album ids for a track: one cached Web API lookup, shared by concurrent callers."""
        cached = self._track_cache.get(track_id)
        if cached and (cached["ok"] or time.monotonic() < cached["retry_at"]):
            self._track_cache.move_to_end(track_id)
            return cached["data"]

        task = self._inflight.get(track_id)
        if task is None:
            task = asyncio.ensure_future(self._fetch_extras(track_id))
            self._inflight[track_id] = task
            task.add_done_callback(lambda _t: self._inflight.pop(track_id, None))
        return await asyncio.shield(task)

    async def _fetch_extras(self, track_id: str) -> dict:
        try:
            info = await self._spotify.get_track_info(track_id)
            entry = {"ok": True, "data": {"artists": info["artist"], "albumid": info["albumid"]}}
        except Exception as e:
            logger.error(f"Track lookup failed for {track_id}: {e}")
            entry = {"ok": False, "data": {"artists": [], "albumid": ""}}
        entry["retry_at"] = time.monotonic() + TRACK_RETRY_SECS
        self._track_cache[track_id] = entry
        self._track_cache.move_to_end(track_id)
        while len(self._track_cache) > TRACK_CACHE_SIZE:
            self._track_cache.popitem(last=False)
        return entry["data"]

    async def get_current_playback(self) -> dict:
        """Same shape as the metadata the API has always served: {"current": {...}}."""
        return {"current": (await self.get_playback_snapshot())["current"]}

    @staticmethod
    def _next_track_from_status(status: Optional[dict]) -> list[dict]:
        """Convert go-librespot's optional next_track to the legacy queue shape."""
        track = status.get("next_track") if status else None
        if not track:
            return []
        uri = track.get("uri") or ""
        return [{
            "song": track.get("name") or "",
            "songid": uri.rsplit(":", 1)[-1] if uri else "",
            "artist": [{"name": name, "id": ""} for name in track.get("artist_names") or []],
            "album": track.get("album_name") or "",
            "albumid": "",
            "cover": cover_proxy_url(track.get("album_cover_url")) or "",
            "year": _year(track.get("release_date")),
        }]

    async def get_playback_snapshot(self) -> dict:
        """Read current and next track from one go-librespot /status response."""
        status = await self.get_status()
        track = status.get("track") if status else None
        if not track:
            return {
                "current": _empty_metadata()["current"],
                "queue": self._next_track_from_status(status),
            }

        uri = track.get("uri") or ""
        songid = uri.split(":")[-1]
        extras = {"artists": [], "albumid": ""}
        if uri.startswith("spotify:track:") and songid:
            extras = await self._track_extras(songid)

        ids_by_name = {a["name"]: a["id"] for a in extras["artists"]}
        artists = [
            {"name": name, "id": ids_by_name.get(name, "")}
            for name in track.get("artist_names") or []
        ]
        current = {
            "artist": artists,
            "song": track.get("name") or "",
            "album": track.get("album_name") or "",
            "songid": songid,
            "albumid": extras["albumid"],
            "cover": cover_proxy_url(track.get("album_cover_url")) or "",
            "year": _year(track.get("release_date")),
            "playing": not status.get("paused", False) and not status.get("stopped", False),
            "duration_ms": track.get("duration") or 0,
            "progress_ms": track.get("position") or 0,
        }
        return {"current": current, "queue": self._next_track_from_status(status)}

    async def add_to_queue(self, track_id: str) -> None:
        try:
            resp = await self._http.post(
                "/player/add_to_queue", json={"uri": f"spotify:track:{track_id}"}
            )
        except httpx.HTTPError as e:
            raise ValueError(f"Playback service unreachable: {type(e).__name__}")
        if not resp.is_success:
            raise ValueError(f"Failed to add track to queue (HTTP {resp.status_code})")

    async def skip(self) -> None:
        try:
            resp = await self._http.post("/player/next")
        except httpx.HTTPError as e:
            raise ValueError(f"Playback service unreachable: {type(e).__name__}")
        if not resp.is_success:
            raise ValueError(f"Failed to skip track (HTTP {resp.status_code})")
