"""Shared setup for the API tests: environment, app import and base test cases.

The environment is set before the app modules are imported because storage,
spotify, librespot and sse read it at import time. Nothing here touches the
network: Spotify, go-librespot and Turnstile calls are stubbed per test, and
the service URLs point at a closed local port in case a stub is missed.
"""
import asyncio
import atexit
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

API_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if API_DIR not in sys.path:
    sys.path.insert(0, API_DIR)

TMP_DIR = tempfile.mkdtemp(prefix="jmm-api-tests-")
atexit.register(shutil.rmtree, TMP_DIR, ignore_errors=True)

os.environ["DB_FILE"] = os.path.join(TMP_DIR, "import.db")
os.environ["TOKEN_CACHE_FILE"] = os.path.join(TMP_DIR, "token_cache.json")
os.environ["SPOTIFY_CLIENT_ID"] = "test-client-id"
os.environ["SPOTIFY_CLIENT_SECRET"] = "test-client-secret"
os.environ["TURNSTILE_SECRET"] = ""
os.environ["LIBRESPOT_API_URL"] = "http://127.0.0.1:9"
os.environ["AUDIO_CONTROL_URL"] = "http://127.0.0.1:9"

from fastapi.testclient import TestClient  # noqa: E402
from starlette.requests import Request  # noqa: E402

import main  # noqa: E402
import sse  # noqa: E402
import storage  # noqa: E402

AUTH_KEY = "test-admin-key"
ADMIN = {"X-Auth-Key": AUTH_KEY}
CLIENT_IP = "203.0.113.10"

TRACK_INFO = {
    "song": "Test Song",
    "songid": "track1",
    "artist": [{"id": "a1", "name": "Artist A"}, {"id": "a2", "name": "Artist B"}],
    "album": "Test Album",
    "albumid": "al1",
    "cover": "https://api.example.test/cover/abc",
}


def track(songid="song1", playing=True, progress_ms=0, **extra):
    """A go-librespot style "current" payload."""
    current = {
        "artist": [{"name": "Artist", "id": ""}],
        "song": f"Song {songid}",
        "album": "Album",
        "songid": songid,
        "albumid": "",
        "cover": "",
        "year": "2024",
        "playing": playing,
        "duration_ms": 200000,
        "progress_ms": progress_ms,
    }
    current.update(extra)
    return current


def reset_state():
    """Clear the in-memory state shared by storage, sse and main."""
    storage.listeners.clear()
    storage.vote_skips.clear()
    storage.rate_limits.clear()
    storage.rate_limit_windows.clear()
    storage.submission_ids.clear()
    storage.play_history.clear()
    storage.upcoming_queue.clear()
    sse.broadcaster.clients.clear()
    sse.realtime_broadcaster.clients.clear()
    sse.current_song_state.update(songid="", started_at=None)
    sse.delayed_current_metadata.clear()
    sse.deck_state.update(songid="", side="left")
    sse.spotify_playing["value"] = None
    sse.latest_snapshot.update(current=None, queue=[], polled_at=None, monotonic=None)
    sse.live_input_state.update(live=False, realtime=False)


def add_listener(listener_id, display=False, ip=CLIENT_IP):
    storage.listeners[listener_id] = {
        "name": None,
        "ip": ip,
        "last_seen": 0,
        "user_agent": "test",
        "volume": None,
        "uuid": listener_id,
        "display": display,
        "theme": None,
        "effects": None,
    }


def parse_sse(chunk):
    """Split one "event: x\\ndata: {...}\\n\\n" chunk into (event, data)."""
    event, data = None, None
    for line in chunk.strip().split("\n"):
        if line.startswith("event: "):
            event = line[len("event: "):]
        elif line.startswith("data: "):
            data = json.loads(line[len("data: "):])
    return event, data


def make_request(path, query=b""):
    return Request({
        "type": "http",
        "method": "GET",
        "path": path,
        "query_string": query,
        "headers": [],
    })


class StateTestCase(unittest.TestCase):
    """Resets in-memory state around each test."""

    def setUp(self):
        reset_state()
        self.addCleanup(reset_state)

    def patch(self, target, name, value):
        patcher = mock.patch.object(target, name, value)
        patcher.start()
        self.addCleanup(patcher.stop)
        return value

    def run_async(self, coro, timeout=5):
        return asyncio.run(asyncio.wait_for(coro, timeout))


class ApiTestCase(StateTestCase):
    """A fresh SQLite database per test and every external call stubbed."""

    def setUp(self):
        super().setUp()
        fd, self.db_path = tempfile.mkstemp(suffix=".db", dir=TMP_DIR)
        os.close(fd)
        self.addCleanup(self._remove_db)
        self.patch(storage, "DB_FILE", self.db_path)
        storage.init_db()

        self.patch(main, "AUTH_KEY", AUTH_KEY)
        self.turnstile = self.patch(main, "verify_turnstile", mock.AsyncMock(return_value=True))
        self.track_info = self.patch(
            main.spotify_client, "get_track_info", mock.AsyncMock(return_value=dict(TRACK_INFO))
        )
        self.patch(main.spotify_client, "search", mock.AsyncMock(return_value=[]))

        self.playback = {"current": track("song1")}
        self.patch(
            main.librespot, "get_current_playback",
            mock.AsyncMock(side_effect=lambda: self.playback),
        )
        self.patch(
            main.librespot, "get_playback_snapshot",
            mock.AsyncMock(side_effect=lambda: {"current": self.playback["current"], "queue": []}),
        )
        self.add_to_queue = self.patch(main.librespot, "add_to_queue", mock.AsyncMock())
        self.skip = self.patch(main.librespot, "skip", mock.AsyncMock())
        self.get_status = self.patch(main.librespot, "get_status", mock.AsyncMock(return_value=None))

        self.client = self.client_for(CLIENT_IP)

    def _remove_db(self):
        try:
            os.remove(self.db_path)
        except OSError:
            pass

    def client_for(self, ip):
        """A TestClient whose requests arrive from ``ip`` (no lifespan is run)."""
        client = TestClient(main.app, client=(ip, 50000))
        self.addCleanup(client.close)
        return client

    def db_rows(self, sql, params=()):
        conn = storage.get_db()
        try:
            return [dict(r) for r in conn.execute(sql, params).fetchall()]
        finally:
            conn.close()

    def set_auto_approve(self, enabled):
        resp = self.client.post("/admin/auto-approve", json={"enabled": enabled}, headers=ADMIN)
        self.assertEqual(resp.status_code, 200)

    def post_request(self, client=None, uri="track1", submission_id=None, uuid=None, name="Tester"):
        payload = {
            "uri": uri,
            "name": name,
            "submission_id": submission_id or os.urandom(8).hex(),
            "turnstile": "token",
        }
        if uuid is not None:
            payload["uuid"] = uuid
        return (client or self.client).post("/request", json=payload)

    def ban(self, **payload):
        resp = self.client.post("/admin/bans", json=payload, headers=ADMIN)
        self.assertEqual(resp.status_code, 200, resp.text)
