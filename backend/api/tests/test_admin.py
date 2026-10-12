"""Admin endpoints: auth, request moderation, bans and song bans."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import support  # noqa: E402
from support import ADMIN, AUTH_KEY, CLIENT_IP, ApiTestCase, add_listener, main, storage  # noqa: E402

REQUEST_COLUMNS = {
    "id", "spotify_uri", "track_name", "artist_name", "album_name", "cover_url",
    "requester_name", "requester_ip", "status", "created_at",
}


class AdminAuthTests(ApiTestCase):
    ENDPOINTS = [
        ("GET", "/admin/requests"),
        ("POST", "/admin/requests/1/approve"),
        ("DELETE", "/admin/requests/1"),
        ("GET", "/admin/listeners"),
        ("GET", "/admin/settings"),
        ("GET", "/admin/bans"),
        ("DELETE", "/admin/bans/1"),
        ("GET", "/admin/song-bans"),
        ("DELETE", "/admin/song-bans/1"),
    ]

    def test_key_required(self):
        for method, path in self.ENDPOINTS:
            with self.subTest(method=method, path=path):
                self.assertEqual(self.client.request(method, path).status_code, 403)
                resp = self.client.request(method, path, headers={"X-Auth-Key": "wrong"})
                self.assertEqual(resp.status_code, 403)

    def test_key_accepted_from_header_or_query(self):
        self.assertEqual(self.client.get("/admin/requests", headers=ADMIN).status_code, 200)
        self.assertEqual(self.client.get(f"/admin/requests?key={AUTH_KEY}").status_code, 200)

    def test_post_endpoints_require_key(self):
        resp = self.client.post("/admin/bans", json={"ip": "192.0.2.1"})
        self.assertEqual(resp.status_code, 403)
        resp = self.client.post("/admin/auto-approve", json={"enabled": False})
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(self.db_rows("SELECT * FROM bans"), [])


class AdminRequestTests(ApiTestCase):
    def pending_request(self, uri="track1"):
        self.set_auto_approve(False)
        self.assertEqual(self.post_request(uri=uri).status_code, 200)
        return self.db_rows("SELECT id FROM requests WHERE spotify_uri=?", (uri,))[0]["id"]

    def listed(self):
        resp = self.client.get("/admin/requests", headers=ADMIN)
        self.assertEqual(resp.status_code, 200)
        return {r["spotify_uri"]: r for r in resp.json()}

    def test_settings_reflect_auto_approve(self):
        self.assertEqual(
            self.client.get("/admin/settings", headers=ADMIN).json(), {"auto_approve": "true"}
        )
        self.set_auto_approve(False)
        self.assertEqual(
            self.client.get("/admin/settings", headers=ADMIN).json(), {"auto_approve": "false"}
        )

    def test_list_includes_columns_and_ban_flags(self):
        self.assertEqual(self.post_request(uri="song1").status_code, 200)  # approved
        row = self.listed()["song1"]
        self.assertEqual(set(row), REQUEST_COLUMNS | {"requester_banned", "song_banned"})
        self.assertIs(row["requester_banned"], False)
        self.assertIs(row["song_banned"], False)
        self.assertEqual(row["status"], "approved")

        self.ban(ip=CLIENT_IP)
        self.client.post("/admin/song-bans", json={"songid": "song1"}, headers=ADMIN)
        row = self.listed()["song1"]
        self.assertIs(row["requester_banned"], True)
        self.assertIs(row["song_banned"], True)

    def test_approve(self):
        request_id = self.pending_request("track9")
        resp = self.client.post(f"/admin/requests/{request_id}/approve", headers=ADMIN)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {"success": True})
        self.add_to_queue.assert_awaited_once_with("track9")
        self.get_status.assert_awaited()
        self.assertEqual(self.listed()["track9"]["status"], "approved")

    def test_approve_missing_request(self):
        resp = self.client.post("/admin/requests/999/approve", headers=ADMIN)
        self.assertEqual(resp.status_code, 404)

    def test_approve_queue_failure_keeps_pending(self):
        request_id = self.pending_request()
        self.add_to_queue.side_effect = ValueError("Playback service unreachable")
        resp = self.client.post(f"/admin/requests/{request_id}/approve", headers=ADMIN)
        self.assertEqual(resp.status_code, 500)
        self.assertEqual(self.listed()["track1"]["status"], "pending")

    def test_delete(self):
        request_id = self.pending_request()
        resp = self.client.delete(f"/admin/requests/{request_id}", headers=ADMIN)
        self.assertEqual(resp.json(), {"success": True})
        self.assertNotIn("track1", self.listed())
        rows = self.db_rows("SELECT status FROM requests WHERE id=?", (request_id,))
        self.assertEqual(rows, [{"status": "deleted"}])

    def test_song_ban_removes_pending_requests(self):
        request_id = self.pending_request("track5")
        self.client.post(
            "/admin/song-bans", json={"songid": "spotify:track:track5"}, headers=ADMIN
        )
        self.assertNotIn("track5", self.listed())
        rows = self.db_rows("SELECT status FROM requests WHERE id=?", (request_id,))
        self.assertEqual(rows, [{"status": "deleted"}])

    def test_listeners_show_ban_and_vote_flags(self):
        add_listener("listener-a", ip=CLIENT_IP)
        add_listener("listener-b", ip="192.0.2.50")
        storage.vote_skips["song1"] = {"listener-b:192.0.2.50"}
        self.ban(ip=CLIENT_IP)
        resp = self.client.get("/admin/listeners", headers=ADMIN)
        by_id = {item["uuid"]: item for item in resp.json()}
        self.assertIs(by_id["listener-a"]["banned"], True)
        self.assertIs(by_id["listener-a"]["voted_skip"], False)
        self.assertIs(by_id["listener-b"]["banned"], False)
        self.assertIs(by_id["listener-b"]["voted_skip"], True)


class AdminBanTests(ApiTestCase):
    def bans(self):
        resp = self.client.get("/admin/bans", headers=ADMIN)
        self.assertEqual(resp.status_code, 200)
        return resp.json()

    def test_ban_crud(self):
        self.ban(ip=" 192.0.2.1 ", reason=" spam ")
        bans = self.bans()
        self.assertEqual(len(bans), 1)
        self.assertEqual(bans[0]["ip"], "192.0.2.1")
        self.assertEqual(bans[0]["uuid"], "")
        self.assertEqual(bans[0]["reason"], "spam")
        self.assertIsNone(bans[0]["expires_at"])

        resp = self.client.delete(f"/admin/bans/{bans[0]['id']}", headers=ADMIN)
        self.assertEqual(resp.json(), {"success": True})
        self.assertEqual(self.bans(), [])

    def test_temporary_ban_has_expiry(self):
        self.ban(uuid="listener-x", duration_minutes=10)
        bans = self.bans()
        self.assertEqual(bans[0]["uuid"], "listener-x")
        self.assertIsNotNone(bans[0]["expires_at"])

    def test_ban_needs_ip_or_uuid(self):
        resp = self.client.post("/admin/bans", json={"reason": "nothing"}, headers=ADMIN)
        self.assertEqual(resp.status_code, 400)

    def test_listing_sweeps_expired_bans(self):
        conn = storage.get_db()
        try:
            conn.execute(
                """INSERT INTO bans (ip, uuid, reason, expires_at)
                   VALUES ('192.0.2.7', '', '', datetime('now', '-1 minutes'))"""
            )
            conn.commit()
        finally:
            conn.close()
        self.assertEqual(self.bans(), [])
        self.assertEqual(self.db_rows("SELECT * FROM bans"), [])

    def test_song_ban_crud(self):
        payload = {"songid": "spotify:track:abc", "track_name": " Title ", "artist_name": "Artist"}
        self.assertEqual(
            self.client.post("/admin/song-bans", json=payload, headers=ADMIN).json(),
            {"success": True},
        )
        self.client.post("/admin/song-bans", json=payload, headers=ADMIN)  # ignored duplicate
        bans = self.client.get("/admin/song-bans", headers=ADMIN).json()
        self.assertEqual(len(bans), 1)
        self.assertEqual(bans[0]["songid"], "abc")
        self.assertEqual(bans[0]["track_name"], "Title")
        self.assertEqual(bans[0]["artist_name"], "Artist")

        resp = self.client.delete(f"/admin/song-bans/{bans[0]['id']}", headers=ADMIN)
        self.assertEqual(resp.json(), {"success": True})
        self.assertEqual(self.client.get("/admin/song-bans", headers=ADMIN).json(), [])

    def test_song_ban_needs_id(self):
        resp = self.client.post("/admin/song-bans", json={"songid": "  "}, headers=ADMIN)
        self.assertEqual(resp.status_code, 400)


class AdminBatchQueryTests(ApiTestCase):
    """Ban checks in the admin lists load bans once per call, not once per row."""

    def count_queries(self, call):
        statements = []
        real_get_db = storage.get_db

        def traced_get_db():
            conn = real_get_db()
            conn.set_trace_callback(statements.append)
            return conn

        self.patch(storage, "get_db", traced_get_db)  # storage functions run in worker threads
        call()
        return len([s for s in statements if s.strip().upper().startswith(("SELECT", "INSERT", "UPDATE", "DELETE"))])

    def seed_requests(self, count, offset):
        conn = storage.get_db()
        try:
            for i in range(offset, offset + count):
                conn.execute(
                    "INSERT INTO requests (spotify_uri, requester_ip, status) VALUES (?, ?, 'pending')",
                    (f"track{i}", f"198.51.100.{i % 250}"),
                )
            conn.commit()
        finally:
            conn.close()

    def test_request_list_query_count_is_constant(self):
        self.seed_requests(2, 0)
        for i in range(5):
            self.ban(ip=f"192.0.2.{i}")
        few = self.count_queries(lambda: self.client.get("/admin/requests", headers=ADMIN))
        self.seed_requests(40, 2)
        for i in range(5, 40):
            self.ban(ip=f"192.0.2.{i}")
        many = self.count_queries(lambda: self.client.get("/admin/requests", headers=ADMIN))
        self.assertEqual(few, many)
        self.assertEqual(many, 3)  # requests, bans, song_bans

    def test_listener_list_query_count_is_constant(self):
        for i in range(2):
            add_listener(f"00000000-0000-4000-8000-00000000000{i}")
        few = self.count_queries(lambda: self.client.get("/admin/listeners", headers=ADMIN))
        for i in range(2, 30):
            add_listener(f"00000000-0000-4000-8000-0000000000{i:02d}")
        many = self.count_queries(lambda: self.client.get("/admin/listeners", headers=ADMIN))
        self.assertEqual(few, many)
        self.assertEqual(many, 1)  # bans


if __name__ == "__main__":
    support.unittest.main()
