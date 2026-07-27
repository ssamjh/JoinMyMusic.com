import logging
from typing import Optional

import httpx

import os

# The TidaLuna bridge plugin running inside the TIDAL desktop client. It binds
# 0.0.0.0 on the DJ box; from inside Docker Desktop the host is reachable as
# host.docker.internal (a 127.0.0.1 bind would NOT be reachable that way).
# Unauthenticated by design — it only listens on a secured subnet.
TIDAL_BRIDGE_URL = os.environ.get("TIDAL_BRIDGE_URL", "http://host.docker.internal:24124").rstrip("/")

logger = logging.getLogger(__name__)

_EMPTY_METADATA = {
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


class TidalClient:
    """Control surface for the TIDAL desktop client, via the Luna bridge plugin.

    There is no OAuth dance here — the desktop client owns the session, and
    this class only talks to the plugin embedded in it.
    """

    def __init__(self):
        self._client = httpx.AsyncClient(base_url=TIDAL_BRIDGE_URL, timeout=5.0)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _get(self, path: str, **params) -> Optional[dict]:
        """GET from the bridge, returning None on any failure.

        The bridge is only up while the TIDAL client is running and still
        modded, so an unreachable bridge is a normal state, not an error to
        propagate — callers fall back to "nothing playing".
        """
        try:
            resp = await self._client.get(path, params=params or None)
            resp.raise_for_status()
            return resp.json()
        except (httpx.HTTPError, ValueError) as e:
            logger.error(f"Tidal bridge GET {path} failed: {e}")
            return None

    async def _post(self, path: str, payload: Optional[dict] = None) -> dict:
        """POST to the bridge, raising ValueError on failure.

        Queue/skip are user-visible actions, so unlike _get these surface the
        failure rather than silently doing nothing.
        """
        try:
            resp = await self._client.post(path, json=payload or {})
            resp.raise_for_status()
            return resp.json() if resp.content else {}
        except httpx.HTTPError as e:
            raise ValueError(f"TIDAL client unavailable: {e}")

    async def get_current_playback(self) -> dict:
        data = await self._get("/playback")
        if not data or "current" not in data:
            return _EMPTY_METADATA
        # Normalise against the canonical shape so a partial payload from a
        # mismatched plugin version can't hand the poll loop missing keys.
        current = dict(_EMPTY_METADATA["current"])
        current.update({k: v for k, v in data["current"].items() if k in current})
        return {"current": current}

    async def search(self, query: str) -> list:
        data = await self._get("/search", q=query)
        if not data:
            raise ValueError("TIDAL client unavailable — can't search right now.")
        return data.get("results", [])

    async def add_to_queue(self, track_id: str) -> None:
        await self._post("/queue", {"id": str(track_id)})

    async def skip(self) -> None:
        await self._post("/next")

    async def get_track_info(self, track_id: str) -> dict:
        data = await self._get(f"/track/{track_id}")
        if not data:
            raise ValueError(f"Could not look up track {track_id}")
        return data

    async def is_authenticated(self) -> bool:
        """True when the bridge answers and TIDAL is logged in.

        This is what surfaces a de-modded client: a TIDAL auto-update installs
        into a fresh app-<version> directory and leaves the Luna patch behind,
        after which the bridge simply stops answering.
        """
        data = await self._get("/health")
        return bool(data and data.get("loggedIn"))
