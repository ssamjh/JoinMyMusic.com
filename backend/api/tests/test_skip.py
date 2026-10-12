"""Skip voting: threshold, duplicates, song matching and display exclusion."""
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import support  # noqa: E402
from support import ApiTestCase, add_listener, storage, track  # noqa: E402


class SkipTests(ApiTestCase):
    def vote(self, listener_id, songid="song1"):
        return self.client.post(
            "/skip", json={"uuid": listener_id, "songid": songid, "turnstile": "token"}
        )

    def stats(self):
        resp = self.client.get("/skip/stats")
        self.assertEqual(resp.status_code, 200)
        return resp.json()

    def test_threshold_is_half_the_room_and_at_least_two(self):
        for n in range(0, 9):
            with self.subTest(listeners=n):
                storage.listeners.clear()
                for i in range(n):
                    add_listener(f"listener-{i}")
                self.assertEqual(self.stats()["needed"], max(2, math.ceil(n / 2)))

    def test_display_listeners_do_not_count(self):
        for i in range(4):
            add_listener(f"person-{i}")
        for i in range(10):
            add_listener(f"screen-{i}", display=True)
        self.assertEqual(self.stats()["needed"], 2)

    def test_votes_until_skip(self):
        for i in range(5):
            add_listener(f"listener-{i}")  # needed = 3
        resp = self.vote("listener-0")
        self.assertEqual(
            resp.json(), {"success": True, "message": "Vote recorded", "count": 1, "needed": 3}
        )
        self.assertEqual(self.stats(), {"song": "song1", "count": 1, "needed": 3})
        self.assertEqual(self.vote("listener-1").json()["count"], 2)
        self.skip.assert_not_awaited()

        resp = self.vote("listener-2")
        self.assertEqual(resp.json(), {"success": True, "message": "Song skipped successfully"})
        self.skip.assert_awaited_once()
        self.assertNotIn("song1", storage.vote_skips)

    def test_duplicate_vote_rejected(self):
        for i in range(5):
            add_listener(f"listener-{i}")
        self.assertEqual(self.vote("listener-0").status_code, 200)
        resp = self.vote("listener-0")
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()["detail"], "You have already voted to skip this song")
        self.assertEqual(self.stats()["count"], 1)

    def test_wrong_song_rejected(self):
        add_listener("listener-0")
        resp = self.vote("listener-0", songid="other-song")
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(
            resp.json()["detail"], "Voted song does not match currently playing song"
        )

    def test_unknown_listener_rejected(self):
        resp = self.vote("not-registered")
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()["detail"], "Invalid or inactive listener")

    def test_skip_failure_is_500(self):
        add_listener("listener-0")
        add_listener("listener-1")
        self.skip.side_effect = ValueError("Failed to skip track")
        self.assertEqual(self.vote("listener-0").status_code, 200)
        self.assertEqual(self.vote("listener-1").status_code, 500)

    def test_stats_with_nothing_playing(self):
        self.playback = {"current": track("", playing=False)}
        self.assertEqual(self.stats(), {"song": "", "count": 0, "needed": 2})


if __name__ == "__main__":
    support.unittest.main()
