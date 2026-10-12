"""POST /request: approval modes, duplicates, rate limiting and bans."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import support  # noqa: E402
from support import ADMIN, CLIENT_IP, ApiTestCase, main, storage  # noqa: E402

AUTO_MESSAGE = "Your request has been automatically approved and added to the queue!"
PENDING_MESSAGE = "Thanks, your request has been added to the queue for approval!"


class RequestTests(ApiTestCase):
    def test_auto_approve_on_queues_and_stores_approved(self):
        resp = self.post_request(uri="track1")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {"success": True, "message": AUTO_MESSAGE})
        self.add_to_queue.assert_awaited_once_with("track1")
        self.get_status.assert_awaited()  # refresh_queue_now read the next track

        rows = self.db_rows("SELECT * FROM requests")
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["status"], "approved")
        self.assertEqual(row["spotify_uri"], "track1")
        self.assertEqual(row["track_name"], "Test Song")
        self.assertEqual(row["artist_name"], "Artist A, Artist B")
        self.assertEqual(row["album_name"], "Test Album")
        self.assertEqual(row["requester_name"], "Tester")
        self.assertEqual(row["requester_ip"], CLIENT_IP)

    def test_auto_approve_off_stores_pending(self):
        self.set_auto_approve(False)
        resp = self.post_request(uri="track2")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {"success": True, "message": PENDING_MESSAGE})
        self.add_to_queue.assert_not_awaited()
        rows = self.db_rows("SELECT status FROM requests")
        self.assertEqual(rows, [{"status": "pending"}])

    def test_track_lookup_failure_still_stores_request(self):
        self.track_info.side_effect = RuntimeError("lookup failed")
        resp = self.post_request(uri="track3")
        self.assertEqual(resp.status_code, 200)
        row = self.db_rows("SELECT * FROM requests")[0]
        self.assertEqual(row["track_name"], "")
        self.assertEqual(row["artist_name"], "")

    def test_duplicate_submission_id_is_already_received(self):
        first = self.post_request(submission_id="same-id")
        second = self.post_request(submission_id="same-id")
        self.assertEqual(first.json()["message"], AUTO_MESSAGE)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(
            second.json(), {"success": True, "message": "Your request was already received!"}
        )
        self.assertEqual(len(self.db_rows("SELECT id FROM requests")), 1)
        self.add_to_queue.assert_awaited_once()

    def test_rate_limit_after_ten_requests(self):
        for _ in range(10):
            self.assertEqual(self.post_request().status_code, 200)
        resp = self.post_request()
        self.assertEqual(resp.status_code, 429)
        self.assertEqual(len(self.db_rows("SELECT id FROM requests")), 10)
        # The limit is per IP.
        self.assertEqual(self.post_request(client=self.client_for("203.0.113.11")).status_code, 200)

    def test_failed_turnstile_is_rejected(self):
        self.turnstile.return_value = False
        resp = self.post_request()
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()["detail"], "Challenge verification failed")
        self.assertEqual(self.db_rows("SELECT id FROM requests"), [])

    def test_missing_name_is_rejected(self):
        resp = self.post_request(name="")
        self.assertEqual(resp.status_code, 400)


class RequestBanTests(ApiTestCase):
    def assertBanned(self, resp):
        self.assertEqual(resp.status_code, 403, resp.text)
        self.assertEqual(resp.json()["detail"], main.BAN_MESSAGE)

    def test_banned_ip(self):
        self.ban(ip=CLIENT_IP)
        self.assertBanned(self.post_request())
        self.assertEqual(self.post_request(client=self.client_for("203.0.113.99")).status_code, 200)

    def test_banned_uuid_matches_from_any_ip(self):
        listener_id = "6f1c2c1e-1111-4222-8333-444455556666"
        self.ban(uuid=listener_id)
        other = self.client_for("198.51.100.20")
        self.assertBanned(self.post_request(client=other, uuid=listener_id))
        self.assertEqual(self.post_request(client=other).status_code, 200)

    def test_banned_ipv4_cidr(self):
        self.ban(ip="198.51.100.0/24")
        self.assertBanned(self.post_request(client=self.client_for("198.51.100.77")))
        self.assertEqual(self.post_request(client=self.client_for("198.51.101.1")).status_code, 200)

    def test_ipv6_ban_covers_the_whole_64(self):
        self.ban(ip="2001:db8:1:2::5")
        bans = self.client.get("/admin/bans", headers=ADMIN).json()
        self.assertEqual(bans[0]["ip"], "2001:db8:1:2::/64")
        self.assertBanned(self.post_request(client=self.client_for("2001:db8:1:2:ffff::1")))
        self.assertEqual(
            self.post_request(client=self.client_for("2001:db8:1:3::1")).status_code, 200
        )

    def test_banned_song(self):
        resp = self.client.post(
            "/admin/song-bans", json={"songid": "spotify:track:badsong"}, headers=ADMIN
        )
        self.assertEqual(resp.status_code, 200)
        for uri in ("badsong", "spotify:track:badsong"):
            resp = self.post_request(uri=uri)
            self.assertEqual(resp.status_code, 403)
            self.assertEqual(resp.json()["detail"], "This song can't be requested.")
        self.assertEqual(self.post_request(uri="goodsong").status_code, 200)

    def test_temporary_ban_matches_until_it_expires(self):
        self.ban(ip=CLIENT_IP, duration_minutes=5)
        self.assertBanned(self.post_request())

    def test_expired_ban_no_longer_matches(self):
        conn = storage.get_db()
        try:
            conn.execute(
                """INSERT INTO bans (ip, uuid, reason, expires_at)
                   VALUES (?, '', '', datetime('now', '-1 minutes'))""",
                (CLIENT_IP,),
            )
            conn.commit()
        finally:
            conn.close()
        self.assertFalse(storage.is_banned(CLIENT_IP))
        self.assertEqual(self.post_request().status_code, 200)


if __name__ == "__main__":
    support.unittest.main()
