import asyncio
import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

METADATA_DELAY = float(os.environ.get("METADATA_DELAY", "5"))
METADATA_DELAY_MS = METADATA_DELAY * 1000

# A jump in Spotify's reported position larger than this (vs. where continuous
# playback would have us) is treated as a seek and re-anchors the song. Kept
# well above normal poll/latency jitter so steady playback never trips it.
SEEK_THRESHOLD_MS = 3000

# The wall-clock instant (ISO 8601) at which the *currently heard* song was at
# position 0 — i.e. the anchor clients reconstruct playback progress from. It is
# expressed in stream time: the audio stream lags Spotify by METADATA_DELAY
# seconds, so this is pushed back by that delay. Every consumer (SSE stream, new
# connections, REST fallback) reports the same anchor.
current_song_state: dict = {"songid": "", "started_at": None}


def stream_started_at(polled_at: datetime, progress_ms: float) -> datetime:
    """Wall-clock instant the currently-heard song was at position 0.

    The listener hears audio METADATA_DELAY seconds behind Spotify's live
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


async def poll_spotify(spotify_client, listeners_state: dict, vote_skips_state: dict):
    """Background task: poll Spotify every 5s, broadcast metadata on change."""
    from storage import add_to_history, get_history, prune_history

    last_stable: dict | None = None  # last broadcast content, ignoring progress
    last_song_id: str = ""
    last_current: dict | None = None  # metadata of the currently playing song

    while True:
        try:
            metadata = await spotify_client.get_current_playback()
            current = metadata.get("current", {})
            song_id = current.get("songid", "")
            progress_ms = current.get("progress_ms", 0)
            polled_at = datetime.now(timezone.utc)

            # Expire history entries older than the TTL, even if nothing else changed.
            history_changed = prune_history()

            # Compare content without the ever-advancing progress, so steady
            # playback doesn't look like a change on every poll.
            stable = {k: v for k, v in current.items() if k != "progress_ms"}
            content_changed = stable != last_stable

            # Detect an in-track seek: re-derive the anchor from Spotify's live
            # position; a jump past the threshold means someone scrubbed. Only
            # while playing — a paused track's frozen progress would otherwise
            # read as an ever-growing drift and re-broadcast every poll.
            seeked = False
            if (
                current.get("playing")
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

                # A new song started: record the previous one and clear its votes.
                if content_changed and song_id and song_id != last_song_id:
                    if last_song_id and last_current:
                        add_to_history(last_current)
                        history_changed = True
                    if last_song_id:
                        vote_skips_state.pop(last_song_id, None)
                        total_listeners = len(listeners_state)
                        needed = max(2, -(-total_listeners // 2))
                        await broadcaster.broadcast("skipvotes", {"song": song_id, "count": 0, "needed": needed})
                    last_current = current
                if content_changed:
                    last_song_id = song_id

                # Hold the announcement so the song flip / tonearm jump lands in
                # sync with the delayed audio stream the listener actually hears.
                await asyncio.sleep(METADATA_DELAY)
                # (Re)anchor to the live position — on a song change and on a
                # seek alike — compensating for the stream delay.
                if song_id:
                    current_song_state["songid"] = song_id
                    current_song_state["started_at"] = stream_started_at(polled_at, progress_ms).isoformat()
                await broadcaster.broadcast("metadata", enrich_with_timing(current))

            if history_changed:
                await broadcaster.broadcast("history", {"history": get_history()})
        except Exception as e:
            logger.error(f"Spotify poll error: {e}")

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


async def refresh_token_loop(spotify_client):
    """Background task: refresh Spotify token every 5 minutes."""
    while True:
        await asyncio.sleep(300)
        try:
            await spotify_client.refresh_token_if_needed()
        except Exception as e:
            logger.error(f"Token refresh loop error: {e}")
