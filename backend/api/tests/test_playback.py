"""Admin playback controls and the automate hold (routes and controller, no network)."""
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import support  # noqa: E402
from support import ADMIN, ApiTestCase, main, storage, track  # noqa: E402

from automate import (  # noqa: E402
    AutomateController,
    IDLE_POLL_SECS,
    PAUSE_BEFORE_END_MS,
    _events_url,
    _is_wake_event,
    read_crossfade_ms,
)


def status(songid, position, duration=200000, paused=False):
    """A go-librespot /status payload for one track."""
    return {
        "track": {
            "uri": f"spotify:track:{songid}",
            "position": position,
            "duration": duration,
        },
        "paused": paused,
        "stopped": False,
    }


class FakeLibrespot:
    """Records control calls. skip/resume change the status the way go-librespot would."""

    def __init__(self):
        self.track_request_at = 0.0
        self.current = None
        self.calls = []
        self.fail_next_pause = False

    async def get_status(self):
        return self.current

    async def pause(self):
        self.calls.append("pause")
        if self.fail_next_pause:
            self.fail_next_pause = False
            raise ValueError("Failed to pause playback (HTTP 500)")
        self.current = dict(self.current, paused=True)

    async def resume(self):
        self.calls.append("resume")
        self.current = dict(self.current, paused=False)

    async def play_pause(self):
        self.calls.append("play_pause")

    async def seek(self, position_ms):
        self.calls.append(("seek", position_ms))
        self.current = status(self.current["track"]["uri"].split(":")[-1], position_ms,
                              paused=self.current["paused"])

    async def skip(self):
        self.calls.append("skip")
        self.track_request_at = time.monotonic()
        self.current = status("next", 0, paused=True)

    async def previous(self):
        self.calls.append("previous")
        self.track_request_at = time.monotonic()
        self.current = status("prev", 0, paused=True)


class AutomateControllerTests(ApiTestCase):
    def setUp(self):
        super().setUp()
        self.fake = FakeLibrespot()
        self.controller = AutomateController(self.fake)

    def set_automate(self, enabled):
        storage.write_automate(enabled)

    def tick(self):
        return self.run_async(self.controller.tick())

    def test_hold_pauses_just_before_the_end(self):
        self.set_automate(False)
        end = 200000  # status() duration
        self.fake.current = status("X", 100000)
        self.assertEqual(self.tick(), IDLE_POLL_SECS)
        self.assertEqual(self.fake.calls, [])

        # Half a second before the hold point: wake up again soon.
        self.fake.current = status("X", end - PAUSE_BEFORE_END_MS - 500)
        self.assertLess(self.tick(), IDLE_POLL_SECS)
        self.assertEqual(self.fake.calls, [])

        # 200 ms past the hold point: hold now.
        self.fake.current = status("X", end - PAUSE_BEFORE_END_MS + 200)
        self.tick()
        self.assertEqual(self.fake.calls, ["pause"])
        self.assertEqual(self.controller._held_songid, "X")

    def test_unrequested_track_change_is_paused_and_rewound(self):
        self.set_automate(False)
        self.fake.current = status("X", 100000)
        self.tick()
        self.fake.current = status("Y", 0)  # the hold was missed, Y started by itself
        self.tick()
        self.assertEqual(self.fake.calls, ["pause", ("seek", 0)])
        self.assertEqual(self.controller._held_songid, "Y")

    def test_stale_request_does_not_count(self):
        self.set_automate(False)
        self.fake.current = status("X", 100000)
        self.tick()
        self.fake.track_request_at = time.monotonic() - 60
        self.fake.current = status("Y", 0)
        self.tick()
        self.assertEqual(self.fake.calls, ["pause", ("seek", 0)])

    def test_request_is_consumed_by_the_change_it_caused(self):
        self.set_automate(False)
        self.fake.current = status("X", 100000)
        self.tick()
        self.run_async(self.controller.player_action("next"))
        self.tick()  # Y is seen as the requested change
        self.fake.current = status("Z", 0)  # a later change nobody asked for
        self.tick()
        self.assertEqual(self.fake.calls, ["skip", "pause", ("seek", 0)])

    def test_requested_track_change_plays_and_is_held_at_its_end(self):
        self.set_automate(False)
        self.fake.current = status("X", 150000)
        self.tick()
        self.fake.current = status("Y", 0)
        self.fake.track_request_at = time.monotonic()  # as skip() would set it
        self.tick()
        self.assertEqual(self.fake.calls, [])

        self.fake.current = status("Y", 200000 - PAUSE_BEFORE_END_MS + 200)
        self.tick()
        self.assertEqual(self.fake.calls, ["pause"])
        self.assertEqual(self.controller._held_songid, "Y")

    def test_song_started_after_a_restart_is_rewound(self):
        self.set_automate(False)
        self.fake.current = status("Y", 2000)  # first sight of the API, Y just started
        self.tick()
        self.assertEqual(self.fake.calls, ["pause", ("seek", 0)])

    def test_song_seen_mid_way_after_a_restart_is_left_alone(self):
        self.set_automate(False)
        self.fake.current = status("Y", 60000)  # e.g. resumed by an admin
        self.tick()
        self.assertEqual(self.fake.calls, [])

    def test_failed_rewind_is_retried(self):
        self.set_automate(False)
        self.fake.current = status("X", 100000)
        self.tick()
        self.fake.current = status("Y", 0)
        self.fake.fail_next_pause = True
        with self.assertRaises(ValueError):
            self.tick()
        self.tick()  # the change is looked at again
        self.assertEqual(self.fake.calls, ["pause", "pause", ("seek", 0)])
        self.assertEqual(self.fake.current["track"]["position"], 0)
        self.assertTrue(self.fake.current["paused"])

    def test_event_stream_url_and_wake_events(self):
        self.assertEqual(_events_url("http://audio:3678"), "ws://audio:3678/events")
        self.assertEqual(_events_url("https://audio.example:3678"), "wss://audio.example:3678/events")
        self.assertTrue(_is_wake_event('{"type": "playing", "data": {}}'))
        self.assertTrue(_is_wake_event('{"type": "will_play"}'))
        self.assertFalse(_is_wake_event('{"type": "volume", "data": {"value": 3}}'))
        self.assertFalse(_is_wake_event("not json"))
        self.assertFalse(_is_wake_event(b"{}"))

    def test_manual_play_of_held_song_restarts_it(self):
        self.set_automate(False)
        self.fake.current = status("X", 199800, paused=True)
        self.controller._held_songid = "X"
        self.run_async(self.controller.player_action("playpause"))
        self.assertEqual(self.fake.calls, [("seek", 0), "play_pause"])

    def test_manual_play_mid_song_does_not_seek(self):
        self.set_automate(False)
        self.fake.current = status("X", 50000, paused=True)
        self.run_async(self.controller.player_action("playpause"))
        self.assertEqual(self.fake.calls, ["play_pause"])

    def test_playpause_with_automate_on_does_not_seek(self):
        self.fake.current = status("X", 199800, paused=True)
        self.run_async(self.controller.player_action("playpause"))
        self.assertEqual(self.fake.calls, ["play_pause"])

    def test_next_and_prev_are_plain_commands(self):
        self.set_automate(False)
        self.run_async(self.controller.player_action("next"))
        self.run_async(self.controller.player_action("prev"))
        self.assertEqual(self.fake.calls, ["skip", "previous"])

    def test_unknown_action_rejected(self):
        with self.assertRaises(ValueError):
            self.run_async(self.controller.player_action("stop"))

    def test_turning_automate_on_resumes_a_held_song_without_skipping(self):
        self.set_automate(False)
        self.fake.current = status("X", 199800)
        self.tick()
        self.assertEqual(self.fake.calls, ["pause"])
        self.assertEqual(self.controller._held_songid, "X")

        self.run_async(self.controller.set_enabled(True))
        self.assertEqual(self.fake.calls, ["pause", "resume"])
        self.assertFalse(self.fake.current["paused"])
        self.assertEqual(self.fake.current["track"]["uri"], "spotify:track:X")
        self.assertIsNone(self.controller._held_songid)

    def test_turning_automate_on_near_the_end_does_not_skip(self):
        # Regression: turning automate on used to skip the song when paused a few seconds
        # before the end, because that counted as a hold.
        self.set_automate(False)
        self.fake.current = status("X", 195500, paused=True)
        self.run_async(self.controller.set_enabled(True))
        self.assertEqual(self.fake.calls, ["resume"])
        self.assertEqual(self.fake.current["track"]["uri"], "spotify:track:X")

    def test_turning_automate_on_resumes_a_plain_pause_mid_song_without_seek(self):
        self.set_automate(False)
        self.fake.current = status("X", 50000, paused=True)
        self.run_async(self.controller.set_enabled(True))
        self.assertEqual(self.fake.calls, ["resume"])
        self.assertEqual(self.fake.current["track"]["position"], 50000)

    def test_held_song_plays_out_into_the_next_without_a_rewind(self):
        self.set_automate(False)
        self.fake.current = status("X", 199800)
        self.tick()
        self.run_async(self.controller.set_enabled(True))

        # The natural change after the resume: Y starts by itself. Automate is on, so
        # neither the rewind backstop nor a hold applies.
        self.fake.current = status("Y", 0)
        self.tick()
        self.fake.current = status("Y", 1000)
        self.tick()
        self.assertEqual(self.fake.calls, ["pause", "resume"])
        self.assertFalse(self.fake.current["paused"])
        self.assertIsNone(self.controller._held_songid)

    def test_no_pending_hold_fires_after_automate_is_turned_on(self):
        self.set_automate(False)
        # Within the hold window, so the loop would pause it on its next check.
        self.fake.current = status("X", 199800)
        self.run_async(self.controller.set_enabled(True))
        self.tick()
        self.tick()
        self.assertEqual(self.fake.calls, [])

    def test_stale_hold_is_cleared_when_automate_is_turned_on(self):
        self.set_automate(False)
        self.controller._held_songid = "X"
        self.fake.current = status("X", 100000, paused=True)
        self.run_async(self.controller.set_enabled(True))
        self.assertIsNone(self.controller._held_songid)
        self.assertEqual(self.fake.calls, ["resume"])

    def test_turning_automate_on_wakes_the_loop(self):
        self.set_automate(False)
        self.fake.current = status("X", 100000)
        self.controller._wake.clear()
        self.run_async(self.controller.set_enabled(True))
        self.assertTrue(self.controller._wake.is_set())

    def test_turning_automate_on_resumes_a_plain_pause(self):
        self.set_automate(False)
        self.fake.current = status("X", 50000, paused=True)
        self.run_async(self.controller.set_enabled(True))
        self.assertEqual(self.fake.calls, ["resume"])

    def test_turning_automate_on_while_playing_does_nothing(self):
        self.set_automate(False)
        self.fake.current = status("X", 50000)
        self.run_async(self.controller.set_enabled(True))
        self.assertEqual(self.fake.calls, [])

    def test_turning_automate_on_while_playing_near_the_end_does_nothing(self):
        self.set_automate(False)
        self.fake.current = status("X", 199000)
        self.run_async(self.controller.set_enabled(True))
        self.tick()
        self.assertEqual(self.fake.calls, [])
        self.assertFalse(self.fake.current["paused"])

    def test_automate_on_does_not_hold(self):
        self.fake.current = status("X", 199800)
        self.assertEqual(self.tick(), IDLE_POLL_SECS)
        self.assertEqual(self.fake.calls, [])

    def test_poll_delay_lands_near_the_end(self):
        now = {"playing": True, "remaining_ms": 300}
        self.assertAlmostEqual(AutomateController._poll_delay(False, now), 0.05)
        now["remaining_ms"] = 60000
        self.assertEqual(AutomateController._poll_delay(False, now), IDLE_POLL_SECS)
        now["remaining_ms"] = PAUSE_BEFORE_END_MS + 750
        self.assertAlmostEqual(AutomateController._poll_delay(False, now), 0.75)
        now["remaining_ms"] = None
        self.assertEqual(AutomateController._poll_delay(False, now), IDLE_POLL_SECS)


class CrossfadeConfigTests(ApiTestCase):
    def test_reads_crossfade_duration_from_the_spotify_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "spotify.yml")
            with open(path, "w", encoding="utf-8") as f:
                f.write("device_name: x\n# crossfade_duration: 9\ncrossfade_duration: 5000  # ms\n")
            self.assertEqual(read_crossfade_ms(path), 5000)

    def test_missing_config_means_no_crossfade(self):
        with self.assertLogs("automate", "WARNING"):
            self.assertEqual(read_crossfade_ms("/nonexistent/spotify.yml"), 0)


class AutomateStorageTests(ApiTestCase):
    def test_defaults_to_on_and_persists(self):
        self.assertTrue(storage.read_automate())
        storage.write_automate(False)
        self.assertFalse(storage.read_automate())
        storage.write_automate(True)
        self.assertTrue(storage.read_automate())


class PlaybackRouteTests(ApiTestCase):
    def test_new_routes_require_key(self):
        for method, path in [
            ("POST", "/admin/player/next"),
            ("POST", "/admin/automate"),
            ("GET", "/admin/playback"),
        ]:
            with self.subTest(path=path):
                self.assertEqual(self.client.request(method, path, json={"enabled": True}).status_code, 403)

    def test_settings_include_automate_default_on(self):
        self.assertEqual(
            self.client.get("/admin/settings", headers=ADMIN).json()["automate"], "true"
        )

    def test_automate_route_persists(self):
        resp = self.client.post("/admin/automate", json={"enabled": False}, headers=ADMIN)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {"success": True, "automate": "false"})
        self.assertEqual(
            self.client.get("/admin/settings", headers=ADMIN).json()["automate"], "false"
        )

    def test_player_routes_call_librespot(self):
        previous = self.patch(main.librespot, "previous", support.mock.AsyncMock())
        play_pause = self.patch(main.librespot, "play_pause", support.mock.AsyncMock())
        for action in ("next", "prev", "playpause"):
            with self.subTest(action=action):
                resp = self.client.post(f"/admin/player/{action}", headers=ADMIN)
                self.assertEqual(resp.status_code, 200)
                self.assertEqual(resp.json(), {"success": True})
        self.skip.assert_awaited_once()
        previous.assert_awaited_once()
        play_pause.assert_awaited_once()

    def test_unknown_action_is_400(self):
        resp = self.client.post("/admin/player/stop", headers=ADMIN)
        self.assertEqual(resp.status_code, 400)

    def test_playback_failure_is_502(self):
        self.skip.side_effect = ValueError("Failed to skip track (HTTP 500)")
        resp = self.client.post("/admin/player/next", headers=ADMIN)
        self.assertEqual(resp.status_code, 502)
        self.assertEqual(resp.json()["detail"], "Failed to skip track (HTTP 500)")

    def test_playing_state_from_poller(self):
        main.spotify_playing["value"] = True
        self.assertEqual(
            self.client.get("/admin/playback", headers=ADMIN).json(), {"playing": True, "track": None}
        )
        main.spotify_playing["value"] = None

    def set_snapshot(self, age, next_song=None, **current):
        """Pretend the poller read this track `age` seconds ago, with `next_song` queued after it."""
        self.patch(main, "latest_snapshot", {
            "current": track(**current),
            "queue": [next_song] if next_song else [],
            "polled_at": None,
            "monotonic": time.monotonic() - age,
        })

    def test_next_track_comes_from_the_poller_snapshot(self):
        queued = track("song2", playing=False)
        self.set_snapshot(0.5, next_song=queued, songid="song1", playing=True)
        body = self.client.get("/admin/playback", headers=ADMIN).json()
        self.assertEqual(body["next"], {
            "songid": "song2",
            "title": "Song song2",
            "artist": "Artist",
            "album": "Album",
            "cover": "",
        })
        self.skip.assert_not_awaited()

    def test_no_queued_track_gives_null_next(self):
        self.set_snapshot(0.5, songid="song1", playing=True)
        self.assertIsNone(self.client.get("/admin/playback", headers=ADMIN).json()["next"])

    def test_stale_snapshot_has_no_next_track(self):
        self.set_snapshot(main.REALTIME_SNAPSHOT_MAX_AGE + 1, next_song=track("song2"),
                          songid="song1", playing=True)
        self.assertIsNone(self.client.get("/admin/playback", headers=ADMIN).json()["next"])

    def test_track_position_advances_while_playing(self):
        self.set_snapshot(2.0, songid="song1", playing=True, progress_ms=1000)
        body = self.client.get("/admin/playback", headers=ADMIN).json()
        self.assertEqual(body["track"]["songid"], "song1")
        self.assertEqual(body["track"]["title"], "Song song1")
        self.assertEqual(body["track"]["artist"], "Artist")
        self.assertEqual(body["track"]["duration_ms"], 200000)
        self.assertGreaterEqual(body["track"]["progress_ms"], 3000)
        self.assertLess(body["track"]["progress_ms"], 4000)

    def test_paused_track_is_shown_at_its_position(self):
        self.set_snapshot(2.0, songid="song1", playing=False, progress_ms=1000)
        body = self.client.get("/admin/playback", headers=ADMIN).json()
        self.assertEqual(body["track"]["progress_ms"], 1000)

    def test_stale_snapshot_has_no_track(self):
        self.set_snapshot(main.REALTIME_SNAPSHOT_MAX_AGE + 1, songid="song1", playing=True)
        self.assertIsNone(self.client.get("/admin/playback", headers=ADMIN).json()["track"])

    def test_idle_player_has_no_track(self):
        self.set_snapshot(0.0, songid="", playing=False)
        self.assertIsNone(self.client.get("/admin/playback", headers=ADMIN).json()["track"])


if __name__ == "__main__":
    support.unittest.main()
