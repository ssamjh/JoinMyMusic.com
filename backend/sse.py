import asyncio
import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

METADATA_DELAY = float(os.environ.get("METADATA_DELAY", "5"))
METADATA_DELAY_MS = METADATA_DELAY * 1000

# A jump in TIDAL's reported position larger than this (vs. where continuous
# playback would have us) is treated as a seek and re-anchors the song. Kept
# well above normal poll/latency jitter so steady playback never trips it.
SEEK_THRESHOLD_MS = 3000

# The wall-clock instant (ISO 8601) at which the *currently heard* song was at
# position 0 — i.e. the anchor clients reconstruct playback progress from. It is
# expressed in stream time: the audio stream lags TIDAL by METADATA_DELAY
# seconds, so this is pushed back by that delay. Every consumer (SSE stream, new
# connections, REST fallback) reports the same anchor.
current_song_state: dict = {"songid": "", "started_at": None}

# Empty "now playing" payload clients already treat as no track (clear UI).
_EMPTY_CURRENT = {
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


def stream_started_at(polled_at: datetime, progress_ms: float) -> datetime:
    """Wall-clock instant the currently-heard song was at position 0.

    The listener hears audio METADATA_DELAY seconds behind TIDAL's live
    position, so the position in their ears right now is
    ``progress_ms - METADATA_DELAY``. Anchoring to that lets a client rebuild the
    heard position from its own monotonic clock — immune to clock skew.
    """
    heard_ms = progress_ms - METADATA_DELAY_MS
    return polled_at - timedelta(milliseconds=heard_ms)


def enrich_with_timing(current: dict) -> dict:
    """Attach elapsed playback time (ms, stream time) for the current song.

    ``elapsed_ms`` is the position the listener is currently hearing (already
    adjusted for the stream delay). Clients anchor their progress UI to it
    against their own clock. May be negative while a freshly started song is
    still inside the stream's delay buffer — clients clamp that to 0.
    """
    out = {k: v for k, v in current.items() if k != "progress_ms"}
    songid = current.get("songid", "")
    started_at = current_song_state["started_at"]
    if songid and songid == current_song_state["songid"] and started_at:
        start = datetime.fromisoformat(started_at)
        elapsed_ms = (datetime.now(timezone.utc) - start).total_seconds() * 1000
        out["started_at"] = started_at
        out["elapsed_ms"] = elapsed_ms
    else:
        out["started_at"] = None
        out["elapsed_ms"] = None
    return out


def metadata_for_clients(current: dict) -> dict:
    """Client-facing now-playing: empty while TIDAL is paused or idle.

    Paused tracks are parked in history by the poll loop; clients should not
    keep showing them as the current song.
    """
    if not current.get("playing") or not current.get("songid"):
        return enrich_with_timing(dict(_EMPTY_CURRENT))
    return enrich_with_timing(current)


class SSEBroadcaster:
    def __init__(self):
        self.clients: list[asyncio.Queue] = []

    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=100)
        self.clients.append(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue):
        if queue in self.clients:
            self.clients.remove(queue)

    async def broadcast(self, event: str, data: dict):
        message = f"event: {event}\ndata: {json.dumps(data)}\n\n"
        dead = []
        for queue in self.clients:
            try:
                queue.put_nowait(message)
            except asyncio.QueueFull:
                dead.append(queue)
        for queue in dead:
            self.clients.remove(queue)


broadcaster = SSEBroadcaster()


async def poll_tidal(tidal_client, listeners_state: dict, vote_skips_state: dict):
    """Background task: poll TIDAL every 5s, broadcast metadata on change.

    Pause clears the current track for clients and parks it in play history.
    Resume of the same track restores it as current and removes it from history.
    """
    from storage import add_to_history, get_history, prune_history, remove_from_history

    last_stable: dict | None = None  # last TIDAL content, ignoring progress
    last_song_id: str = ""
    last_current: dict | None = None  # metadata of the last active track
    # Song id we moved into history because TIDAL paused (for resume restore).
    parked_for_pause_id: str = ""

    while True:
        try:
            metadata = await tidal_client.get_current_playback()
            current = metadata.get("current", {})
            song_id = current.get("songid", "")
            playing = bool(current.get("playing"))
            progress_ms = current.get("progress_ms", 0)
            polled_at = datetime.now(timezone.utc)

            # Expire history entries older than the TTL, even if nothing else changed.
            history_changed = prune_history()

            # Compare content without the ever-advancing progress, so steady
            # playback doesn't look like a change on every poll.
            stable = {k: v for k, v in current.items() if k != "progress_ms"}
            content_changed = stable != last_stable

            # Detect an in-track seek: re-derive the anchor from TIDAL's live
            # position; a jump past the threshold means someone scrubbed. Only
            # while playing — a paused track's frozen progress would otherwise
            # read as an ever-growing drift and re-broadcast every poll.
            seeked = False
            if (
                playing
                and song_id
                and song_id == current_song_state["songid"]
                and current_song_state["started_at"]
            ):
                candidate = stream_started_at(polled_at, progress_ms)
                stored = datetime.fromisoformat(current_song_state["started_at"])
                if abs((candidate - stored).total_seconds()) * 1000 > SEEK_THRESHOLD_MS:
                    seeked = True

            if content_changed or seeked:
                last_stable = stable

                # Track identity changed (new song, or cleared entirely).
                if content_changed and song_id != last_song_id:
                    # Previous track finished / was skipped: history it unless we
                    # already parked it for a pause (would double-insert).
                    if last_song_id and last_current and not parked_for_pause_id:
                        add_to_history(last_current)
                        history_changed = True
                        # Drop current so the pause branch below does not re-park.
                        if current_song_state["songid"] == last_song_id:
                            current_song_state["songid"] = ""
                            current_song_state["started_at"] = None
                    if last_song_id:
                        vote_skips_state.pop(last_song_id, None)
                        total_listeners = len(listeners_state)
                        needed = max(2, -(-total_listeners // 2))
                        await broadcaster.broadcast(
                            "skipvotes",
                            {"song": song_id, "count": 0, "needed": needed},
                        )
                    if song_id:
                        last_current = current
                        last_song_id = song_id
                    else:
                        last_current = None
                        last_song_id = ""
                    # Keep parked_for_pause_id across paused track-list browsing;
                    # only resume / a new *playing* track clears it (below).

                # Hold the announcement so the song flip / tonearm jump lands in
                # sync with the delayed audio stream the listener actually hears.
                await asyncio.sleep(METADATA_DELAY)

                if playing and song_id:
                    # Playing (or resumed). If this track was parked on pause,
                    # pull it back out of history.
                    if parked_for_pause_id == song_id:
                        if remove_from_history(song_id):
                            history_changed = True
                        parked_for_pause_id = ""
                    elif parked_for_pause_id:
                        # A different track is now playing; leave the parked one
                        # in history.
                        parked_for_pause_id = ""

                    last_current = current
                    last_song_id = song_id
                    current_song_state["songid"] = song_id
                    current_song_state["started_at"] = stream_started_at(
                        polled_at, progress_ms
                    ).isoformat()
                    await broadcaster.broadcast("metadata", enrich_with_timing(current))
                else:
                    # Paused or no playback: clients see empty "now playing".
                    # Park whatever we were actively showing, once (resume restores).
                    if current_song_state["songid"] and not parked_for_pause_id:
                        to_park = None
                        shown_id = current_song_state["songid"]
                        if last_current and last_current.get("songid") == shown_id:
                            to_park = last_current
                        elif song_id == shown_id and current.get("song"):
                            to_park = current
                        if to_park and to_park.get("songid"):
                            add_to_history(to_park)
                            history_changed = True
                            parked_for_pause_id = to_park["songid"]
                            last_current = to_park
                            last_song_id = parked_for_pause_id

                    current_song_state["songid"] = ""
                    current_song_state["started_at"] = None
                    await broadcaster.broadcast(
                        "metadata", enrich_with_timing(dict(_EMPTY_CURRENT))
                    )

            if history_changed:
                await broadcaster.broadcast("history", {"history": get_history()})
        except Exception as e:
            logger.error(f"TIDAL poll error: {e}")

        await asyncio.sleep(5)


async def cleanup_listeners(listeners_state: dict):
    """Background task: expire stale listeners every 30s, broadcast listener count."""
    while True:
        await asyncio.sleep(30)
        try:
            now = time.time()
            expired = [u for u, d in list(listeners_state.items()) if now - d["last_seen"] > 60]
            for u in expired:
                del listeners_state[u]
            await broadcaster.broadcast("listeners", {"count": len(listeners_state)})
        except Exception as e:
            logger.error(f"Listener cleanup error: {e}")


async def cleanup_old_requests():
    """Background task: delete requests older than 12 hours, runs every hour."""
    from storage import cleanup_old_requests as _cleanup
    while True:
        await asyncio.sleep(3600)
        try:
            _cleanup()
        except Exception as e:
            logger.error(f"Request cleanup error: {e}")
