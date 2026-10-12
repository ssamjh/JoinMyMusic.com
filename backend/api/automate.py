"""Automate: whether playback carries on into the next song.

With automate on, go-librespot plays through as usual. With it off, playback holds at the
end of the song that is playing: a pause is issued shortly before the end (before the
crossfade starts), so the next song never starts. Three layers back this up:

- The hold is timed from the song's position and duration, and the loop wakes for it.
- go-librespot's /events stream wakes the loop at once on track and playback events, so a
  change is seen without waiting for the poll interval.
- A track change that no request of ours caused is paused at once and rewound to 0. This
  covers a hold that was missed, including a song that began while the API was down.
"""
import asyncio
import json
import logging
import os
import re
import time
from typing import Optional

import websockets

from librespot import LIBRESPOT_API_URL, LibrespotClient
from storage import read_automate, write_automate

logger = logging.getLogger(__name__)

# go-librespot's config (audio/config/spotify.yml), mounted read-only from the audio config.
SPOTIFY_CONFIG_FILE = os.environ.get("SPOTIFY_CONFIG_FILE", "/etc/jmm/spotify.yml")


def read_crossfade_ms(path: str = SPOTIFY_CONFIG_FILE) -> int:
    """crossfade_duration (ms) from go-librespot's config; 0 if it is unset or unreadable."""
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                match = re.match(r"crossfade_duration:\s*(\d+)\s*(?:#.*)?$", line)
                if match:
                    return int(match.group(1))
    except OSError as exc:
        logger.warning("Could not read %s (%s); assuming no crossfade", path, exc)
    return 0


# The next song fades in over this long before the end, so the hold has to come before
# the crossfade starts.
SPOTIFY_CROSSFADE_MS = read_crossfade_ms()
# Playback is paused this long before the end of the song.
PAUSE_BEFORE_END_MS = SPOTIFY_CROSSFADE_MS + 250
# A paused song this close to its end counts as held at the end (also after a restart).
AT_END_MS = PAUSE_BEFORE_END_MS + 750
# A track change this soon after a skip or previous command was requested by us.
REQUEST_WINDOW_SECS = 10.0
# A song first seen playing this early after a restart, with automate off and no request
# of ours, is rewound to 0 rather than left to run.
FRESH_START_MS = 10000
IDLE_POLL_SECS = 1.0
MIN_POLL_SECS = 0.05
RETRY_SECS = 0.2
EVENT_RECONNECT_SECS = 2.0
# go-librespot /events types that can move playback; each one wakes the loop at once.
WAKE_EVENTS = frozenset(
    {"metadata", "will_play", "playing", "paused", "not_playing", "seek", "stopped"}
)



def _observe(status: Optional[dict]) -> Optional[dict]:
    """The watched fields of a go-librespot /status response, or None without a track."""
    track = status.get("track") if status else None
    if not track:
        return None
    duration = track.get("duration") or 0
    position = track.get("position") or 0
    return {
        "songid": (track.get("uri") or "").split(":")[-1],
        "playing": not status.get("paused", False) and not status.get("stopped", False),
        "position_ms": position,
        "remaining_ms": max(0, duration - position) if duration > 0 else None,
    }


def _events_url(base_url: str) -> str:
    """The go-librespot websocket event stream for an http(s) base URL."""
    scheme, _, rest = base_url.partition("://")
    return ("wss" if scheme == "https" else "ws") + f"://{rest}/events"


def _is_wake_event(message) -> bool:
    if not isinstance(message, str):
        return False
    try:
        event = json.loads(message)
    except ValueError:
        return False
    return isinstance(event, dict) and event.get("type") in WAKE_EVENTS


class AutomateController:
    """Holds playback at song ends while automate is off. All calls share one lock."""

    def __init__(self, librespot: LibrespotClient):
        self._librespot = librespot
        self._lock = asyncio.Lock()
        self._wake = asyncio.Event()
        self._last: Optional[dict] = None  # the last observed song, see _observe
        # Song this loop paused (at its end, or rewound). Cleared as soon as it plays again.
        self._held_songid: Optional[str] = None

    async def run(self) -> None:
        """Background loop; started from the API lifespan."""
        listener = asyncio.create_task(self._listen_events())
        try:
            while True:
                self._wake.clear()
                delay = IDLE_POLL_SECS
                try:
                    delay = await self.tick()
                except Exception:
                    logger.exception("Automate check failed")
                    delay = RETRY_SECS
                try:
                    await asyncio.wait_for(self._wake.wait(), timeout=delay)
                except asyncio.TimeoutError:
                    pass
        finally:
            listener.cancel()

    async def _listen_events(self) -> None:
        """Wake the loop on go-librespot playback events. The poll covers any gap."""
        url = _events_url(LIBRESPOT_API_URL)
        while True:
            try:
                async with websockets.connect(url, open_timeout=5) as ws:
                    self._wake.set()
                    async for message in ws:
                        if _is_wake_event(message):
                            self._wake.set()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.debug(f"go-librespot event stream closed: {e}")
            await asyncio.sleep(EVENT_RECONNECT_SECS)

    async def tick(self) -> float:
        """One check. Returns the number of seconds until the next one."""
        async with self._lock:
            enabled = await asyncio.to_thread(read_automate)
            now_mono = time.monotonic()
            now = _observe(await self._librespot.get_status())
            last, self._last = self._last, now
            try:
                await self._check(enabled, last, now, now_mono)
            except Exception:
                # Look at this song again on the next check, so a failed pause is retried.
                self._last = last
                raise
            if now is None:
                return IDLE_POLL_SECS
            return self._poll_delay(enabled, now)

    async def _check(self, enabled: bool, last: Optional[dict], now: Optional[dict],
                     now_mono: float) -> None:
        if now is None:
            self._held_songid = None
            return

        if last is None or last["songid"] != now["songid"]:
            requested = self._consume_request(now_mono)
            self._held_songid = None
            fresh_start = now["playing"] and now["position_ms"] <= FRESH_START_MS
            if not enabled and not requested and (last is not None or fresh_start):
                # A song started that no request of ours caused: do not let it play on.
                await self._librespot.pause()
                await self._librespot.seek(0)
                self._held_songid = now["songid"]
                now["playing"] = False

        if now["playing"]:
            self._held_songid = None

        if not enabled and now["playing"] and now["remaining_ms"] is not None \
                and now["remaining_ms"] <= PAUSE_BEFORE_END_MS:
            await self._librespot.pause()
            self._held_songid = now["songid"]
            now["playing"] = False

    def _consume_request(self, now_mono: float) -> bool:
        """Whether a track change happening now was requested by a skip or previous command."""
        requested_at = self._librespot.track_request_at
        self._librespot.track_request_at = 0.0
        return requested_at > 0 and now_mono - requested_at <= REQUEST_WINDOW_SECS

    @staticmethod
    def _poll_delay(enabled: bool, now: dict) -> float:
        """Poll once a second, but land close to the end of a song that is playing with automate off."""
        if enabled or not now["playing"] or now["remaining_ms"] is None:
            return IDLE_POLL_SECS
        wait = (now["remaining_ms"] - PAUSE_BEFORE_END_MS) / 1000
        return min(IDLE_POLL_SECS, max(MIN_POLL_SECS, wait))

    def _is_held(self, now: dict) -> bool:
        """Paused at the end of a song: held by this loop, or close enough to the end to be."""
        if self._held_songid == now["songid"]:
            return True
        return now["remaining_ms"] is not None and now["remaining_ms"] <= AT_END_MS

    async def set_enabled(self, enabled: bool) -> None:
        async with self._lock:
            await asyncio.to_thread(write_automate, enabled)
            if enabled:
                await self._resume_if_paused()
            # Re-check now under the new setting rather than at the old poll time.
            self._wake.set()

    async def _resume_if_paused(self) -> None:
        """Turning automate on: resume a paused player, as Play does, and nothing else. No skip
        and no seek. A song held at its end plays out into the next one, which the loop does
        not treat as unrequested while automate is on. A playing player is left alone."""
        # Any hold made while automate was off no longer applies.
        self._held_songid = None
        now = _observe(await self._librespot.get_status())
        if now is not None and not now["playing"]:
            await self._librespot.resume()

    async def player_action(self, action: str) -> None:
        """Transport control for the admin panel: next, prev or playpause."""
        async with self._lock:
            if action == "next":
                await self._librespot.skip()
            elif action == "prev":
                await self._librespot.previous()
            elif action == "playpause":
                if not await asyncio.to_thread(read_automate):
                    now = _observe(await self._librespot.get_status())
                    if now is not None and not now["playing"] and self._is_held(now):
                        # Play from the start rather than from the end, which would
                        # skip straight past the song.
                        await self._librespot.seek(0)
                await self._librespot.play_pause()
            else:
                raise ValueError(f"Unknown playback action: {action}")
