import asyncio
import logging
import os
import time
from datetime import datetime, timedelta, timezone

import httpx

logger = logging.getLogger(__name__)

METADATA_DELAY = float(os.environ.get("METADATA_DELAY", "5"))
METADATA_DELAY_MS = METADATA_DELAY * 1000

# Live input mode: the stream is the VBAN input alone (audio on it while Spotify is not
# playing). The audio container's control API reports the input's level.
AUDIO_CONTROL_URL = os.environ.get("AUDIO_CONTROL_URL", "http://audio:8090").rstrip("/")
LIVE_INPUT_THRESHOLD_DB = -50.0  # RMS over 1 s; above this the input counts as audio
LIVE_INPUT_HOLD = 8.0  # seconds the mode holds through a pause in the input
# Undelayed Spotify state from poll_spotify; None until its first poll.
spotify_playing: dict = {"value": None}
# The mode as heard on the delayed stream ("live") and in real time ("realtime").
live_input_state: dict = {"live": False, "realtime": False}

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
delayed_current_metadata: dict = {}
deck_state: dict = {"songid": "", "side": "left"}

# The poller's latest successful read of go-librespot: the undelayed current track, the
# wall-clock instant it was read, and the monotonic clock at that moment (for age checks).
# New realtime connections and vote lookups reuse it instead of calling go-librespot again.
REALTIME_SNAPSHOT_MAX_AGE = 3.0  # seconds
latest_snapshot: dict = {"current": None, "polled_at": None, "monotonic": None}

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
    out["deck_side"] = current.get("deck_side", deck_state["side"])
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
    """Client-facing now-playing: empty while Spotify is paused or idle.

    Paused tracks are parked in history by the poll loop; clients should not
    keep showing them as the current song.
    """
    if not current.get("playing") or not current.get("songid"):
        return enrich_with_timing(dict(_EMPTY_CURRENT))
    return enrich_with_timing(current)


def committed_metadata_for_clients() -> dict:
    """Return the latest metadata committed for the delayed audio stream."""
    current = delayed_current_metadata or dict(_EMPTY_CURRENT)
    return metadata_for_clients(current)


def live_metadata_for_clients(current: dict, polled_at: datetime | None = None) -> dict:
    """Now-playing payload for realtime consumers: no audio-sync delay.

    Unlike ``metadata_for_clients``, ``elapsed_ms`` is Spotify's live reported
    position, not the stream-delayed position the audio listener actually
    hears. Intended for the realtime SSE stream, not the synced player UI.
    """
    if not current.get("playing") or not current.get("songid"):
        out = {k: v for k, v in _EMPTY_CURRENT.items() if k != "progress_ms"}
        out["started_at"] = None
        out["elapsed_ms"] = None
        out["deck_side"] = deck_state["side"]
        return out
    polled_at = polled_at or datetime.now(timezone.utc)
    progress_ms = current.get("progress_ms", 0)
    out = {k: v for k, v in current.items() if k != "progress_ms"}
    out["deck_side"] = current.get("deck_side", deck_state["side"])
    out["started_at"] = (polled_at - timedelta(milliseconds=progress_ms)).isoformat()
    out["elapsed_ms"] = progress_ms
    return out


# Put on a client's queue when the broadcaster drops it, so its SSE response ends and
# the browser's EventSource reconnects instead of waiting forever on an orphaned queue.
SSE_CLOSED = object()


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
        dead = []
        for queue in self.clients:
            try:
                queue.put_nowait((event, data))
            except asyncio.QueueFull:
                dead.append(queue)
        for queue in dead:
            self.clients.remove(queue)
            # The client is already far behind and is being disconnected, so its
            # undelivered backlog is discarded in favour of the close marker.
            while not queue.empty():
                queue.get_nowait()
            queue.put_nowait(SSE_CLOSED)


broadcaster = SSEBroadcaster()
# Fed the same events as `broadcaster`, except "metadata" is pushed the instant
# Spotify reports a change instead of waiting out METADATA_DELAY — for
# consumers that want live data rather than data synced to the audio stream.
realtime_broadcaster = SSEBroadcaster()


async def broadcast_both(event: str, data: dict):
    await broadcaster.broadcast(event, data)
    await realtime_broadcaster.broadcast(event, data)


async def update_queue(queue) -> bool:
    """Replace the public queue with the single next track from a status snapshot."""
    from storage import upcoming_queue

    # go-librespot's next_track can legitimately be the same song as current
    # (repeat track), so song identity is not a reason to discard it.
    new_queue = list(queue or [])[:1]
    if new_queue == upcoming_queue:
        return False
    upcoming_queue[:] = new_queue
    return True


async def publish_queue_snapshot(queue, track_changed: bool = False) -> bool:
    """Store a status snapshot's next track and notify the appropriate SSE feeds."""
    from storage import upcoming_queue

    changed = await update_queue(queue)
    if changed:
        payload = {"queue": list(upcoming_queue)}
        if track_changed:
            await realtime_broadcaster.broadcast("queue", payload)
        else:
            await broadcast_both("queue", payload)
    return changed


async def current_track_snapshot(librespot) -> tuple[dict, datetime]:
    """The undelayed current track and the wall-clock instant its progress refers to.

    Uses the poller's snapshot while it is fresh (at most REALTIME_SNAPSHOT_MAX_AGE old);
    otherwise asks go-librespot. A fresh snapshot's progress is advanced by its age while
    playing, so the returned pair describes the same position as a direct read would.
    """
    snapshot = latest_snapshot
    if snapshot["current"] is not None:
        age = time.monotonic() - snapshot["monotonic"]
        if age <= REALTIME_SNAPSHOT_MAX_AGE:
            current = dict(snapshot["current"])
            if current.get("playing") and current.get("songid"):
                current["progress_ms"] = current.get("progress_ms", 0) + age * 1000
            return current, datetime.now(timezone.utc)
    metadata = await librespot.get_current_playback()
    return metadata.get("current", {}), datetime.now(timezone.utc)


async def refresh_queue_now(librespot) -> None:
    """Read go-librespot status, then broadcast its next track if it changed."""
    try:
        status = await librespot.get_status()
        queue = librespot._next_track_from_status(status)
    except Exception as e:
        logger.error(f"Queue refresh error: {e}")
        queue = []
    await publish_queue_snapshot(queue)


async def poll_spotify(librespot, listeners_state: dict, vote_skips_state: dict):
    """Poll live status continuously and commit normal metadata after stream delay.

    Realtime metadata is broadcast as soon as go-librespot reports a change.
    Delayed metadata is queued so the poller keeps running during the audio
    buffer delay. Brief inactive snapshots are discarded when playback resumes
    on a track before their delayed clear would reach the stream.
    """
    from storage import (
        active_listener_count,
        add_to_history,
        get_history,
        prune_history,
        remove_from_history,
        upcoming_queue,
    )

    last_stable: dict | None = None  # last Spotify content, ignoring progress
    last_song_id: str = ""
    delayed_song: dict | None = None
    delayed_song_id = ""
    # Song id parked in history because a delayed pause was committed.
    parked_for_pause_id: str = ""
    pending: list[dict] = []
    loop = asyncio.get_running_loop()

    async def commit(item: dict) -> None:
        nonlocal delayed_song, delayed_song_id, parked_for_pause_id
        current = item["current"]
        song_id = current.get("songid", "")
        playing = bool(current.get("playing") and song_id)
        history_changed = False

        if playing:
            if parked_for_pause_id == song_id:
                if remove_from_history(song_id):
                    history_changed = True
                parked_for_pause_id = ""
            elif parked_for_pause_id:
                parked_for_pause_id = ""

            if delayed_song_id and delayed_song_id != song_id and delayed_song:
                add_to_history(delayed_song)
                history_changed = True
            delayed_song = current
            delayed_song_id = song_id
            current_song_state["songid"] = song_id
            current_song_state["started_at"] = stream_started_at(
                item["polled_at"], current.get("progress_ms", 0)
            ).isoformat()
            delayed_current_metadata.clear()
            delayed_current_metadata.update(current)
            await broadcaster.broadcast("metadata", enrich_with_timing(current))
        else:
            # A paused track or confirmed idle state moves the displayed song
            # into history only when the corresponding delayed clear commits.
            if delayed_song_id and delayed_song:
                add_to_history(delayed_song)
                history_changed = True
                if song_id == delayed_song_id:
                    parked_for_pause_id = delayed_song_id
                delayed_song = None
                delayed_song_id = ""
            elif song_id:
                # Handles a pause arriving before any delayed playing snapshot
                # was committed (for example immediately after service startup).
                parked_for_pause_id = song_id

            current_song_state["songid"] = ""
            current_song_state["started_at"] = None
            delayed_current_metadata.clear()
            delayed_current_metadata.update(_EMPTY_CURRENT)
            await broadcaster.broadcast(
                "metadata", enrich_with_timing(dict(_EMPTY_CURRENT))
            )

        if item["queue_changed"]:
            await broadcaster.broadcast("queue", {"queue": list(upcoming_queue)})
        if history_changed:
            await broadcast_both("history", {"history": get_history()})

    async def flush_due() -> None:
        now = loop.time()
        while pending and pending[0]["due"] <= now:
            item = pending.pop(0)
            await commit(item)

    while True:
        try:
            metadata = await librespot.get_playback_snapshot()
            current = dict(metadata.get("current", {}))
            song_id = current.get("songid", "")
            if song_id and current.get("playing") and song_id != deck_state["songid"]:
                if deck_state["songid"]:
                    deck_state["side"] = "right" if deck_state["side"] == "left" else "left"
                deck_state["songid"] = song_id
            current["deck_side"] = deck_state["side"]
            playing = bool(current.get("playing"))
            spotify_playing["value"] = bool(playing and song_id)
            progress_ms = current.get("progress_ms", 0)
            polled_at = datetime.now(timezone.utc)
            latest_snapshot.update(
                current=dict(current), polled_at=polled_at, monotonic=time.monotonic()
            )

            # Expire history entries older than the TTL, even if nothing else changed.
            history_changed = prune_history()

            # Compare content without the ever-advancing progress, so steady
            # playback doesn't look like a change on every poll.
            stable = {k: v for k, v in current.items() if k != "progress_ms"}
            content_changed = stable != last_stable
            track_identity_changed = content_changed and song_id != last_song_id

            # next_track can resolve after the current track is already stable,
            # or an external Spotify client can edit the queue at any time.
            # Keep both SSE feeds current in those cases. For a song transition,
            # the realtime queue moves immediately and the delayed feed follows
            # the audio metadata after METADATA_DELAY below.
            queue_changed = await publish_queue_snapshot(
                metadata.get("queue", []), track_changed=track_identity_changed
            )

            # Detect an in-track seek: re-derive the anchor from Spotify's live
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
                # Realtime consumers get the raw metadata the instant it
                # changes — they don't wait for the audio-sync delay below.
                await realtime_broadcaster.broadcast(
                    "metadata", live_metadata_for_clients(current, polled_at)
                )

                # Track identity changed (new song, or cleared entirely).
                if track_identity_changed:
                    if last_song_id:
                        vote_skips_state.pop(last_song_id, None)
                        total_listeners = active_listener_count(listeners_state)
                        needed = max(2, -(-total_listeners // 2))
                        await broadcast_both(
                            "skipvotes",
                            {"song": song_id, "count": 0, "needed": needed},
                        )
                    if song_id:
                        last_song_id = song_id
                    else:
                        last_song_id = ""
                # If playback returns before a pending inactivity clear reaches
                # the stream, treat that snapshot as a transition gap. Explicit
                # pause events are also coalesced only when a different track
                # resumes; a same-track resume keeps the pause visible.
                if playing and song_id:
                    delayed_queue_change = any(
                        item["queue_changed"] and not item["current"].get("playing")
                        for item in pending
                    )
                    pending[:] = [
                        item for item in pending
                        if item["current"].get("playing")
                        or item["current"].get("songid") == song_id
                    ]
                    queue_changed = queue_changed or delayed_queue_change

                pending.append({
                    "due": loop.time() + METADATA_DELAY,
                    "current": dict(current),
                    "polled_at": polled_at,
                    "queue_changed": bool(queue_changed and track_identity_changed),
                })
                pending.sort(key=lambda item: item["due"])

            if history_changed:
                await broadcast_both("history", {"history": get_history()})
            await flush_due()
        except Exception as e:
            logger.error(f"Playback poll error: {e}")
            await publish_queue_snapshot([])
            await flush_due()

        await asyncio.sleep(1)


async def poll_live_input():
    """Broadcast "source" {"live": bool} when live input mode starts or ends.

    The realtime feed gets the change at once; the normal feed METADATA_DELAY later, so
    it lines up with the delayed audio like the metadata does.
    """
    loop = asyncio.get_running_loop()
    last_heard: float | None = None
    pending: list[tuple[float, bool]] = []
    async with httpx.AsyncClient(base_url=AUDIO_CONTROL_URL, timeout=httpx.Timeout(2.0)) as http:
        while True:
            level = None
            try:
                resp = await http.get("/api/source")
                if resp.status_code == 200:
                    level = resp.json().get("vban_level_db")
            except (httpx.HTTPError, ValueError):
                pass
            now = loop.time()
            if isinstance(level, (int, float)) and level > LIVE_INPUT_THRESHOLD_DB:
                last_heard = now
            live = (
                spotify_playing["value"] is False
                and last_heard is not None
                and now - last_heard < LIVE_INPUT_HOLD
            )
            try:
                if live != live_input_state["realtime"]:
                    live_input_state["realtime"] = live
                    await realtime_broadcaster.broadcast("source", {"live": live})
                    pending.append((now + METADATA_DELAY, live))
                while pending and pending[0][0] <= now:
                    _, value = pending.pop(0)
                    live_input_state["live"] = value
                    await broadcaster.broadcast("source", {"live": value})
            except Exception as e:
                logger.error(f"Live input broadcast error: {e}")
            await asyncio.sleep(1)


async def cleanup_listeners(listeners_state: dict):
    """Background task: every 30s expire stale listeners and request state, broadcast listener count."""
    from storage import active_listener_count, prune_request_state

    while True:
        await asyncio.sleep(30)
        try:
            now = time.time()
            expired = [u for u, d in list(listeners_state.items()) if now - d["last_seen"] > 60]
            for u in expired:
                del listeners_state[u]
            prune_request_state()
            await broadcast_both("listeners", {"count": active_listener_count(listeners_state)})
        except Exception as e:
            logger.error(f"Listener cleanup error: {e}")


async def cleanup_old_requests():
    """Background task: delete requests older than 12 hours, runs every hour."""
    from storage import cleanup_old_requests as _cleanup
    while True:
        await asyncio.sleep(3600)
        try:
            await asyncio.to_thread(_cleanup)
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
