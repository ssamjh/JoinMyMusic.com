import asyncio
import json
import logging
import time

logger = logging.getLogger(__name__)


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
    last_metadata: dict | None = None
    last_song_id: str = ""

    while True:
        try:
            metadata = await spotify_client.get_current_playback()
            current = metadata.get("current", {})
            song_id = current.get("songid", "")

            if metadata != last_metadata:
                last_metadata = metadata

                # Clear votes when song changes
                if song_id and song_id != last_song_id and last_song_id:
                    vote_skips_state.pop(last_song_id, None)
                last_song_id = song_id

                await broadcaster.broadcast("metadata", current)
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
