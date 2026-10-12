"""Host controls for the persistent pending-request order and deleted archive."""
import sqlite3
from concurrent.futures import ThreadPoolExecutor

from support import ADMIN, ApiTestCase, storage


class RequestModerationTests(ApiTestCase):
    def test_new_moderation_routes_require_admin_auth(self):
        self.assertEqual(self.client.get("/admin/requests/deleted").status_code, 403)
        self.assertEqual(
            self.client.post("/admin/requests/1/move", json={"direction": "up"}).status_code,
            403,
        )
        self.assertEqual(self.client.post("/admin/requests/1/restore").status_code, 403)

    def make_pending(self, uri):
        self.set_auto_approve(False)
        self.assertEqual(self.post_request(uri=uri).status_code, 200)
        return self.db_rows("SELECT id FROM requests WHERE spotify_uri=?", (uri,))[0]["id"]

    def pending_uris(self):
        return [
            row["spotify_uri"] for row in self.client.get("/admin/requests", headers=ADMIN).json()
            if row["status"] == "pending"
        ]

    def test_move_bounds_and_persisted_order(self):
        first = self.make_pending("first")
        second = self.make_pending("second")
        third = self.make_pending("third")
        self.assertEqual(self.pending_uris(), ["first", "second", "third"])

        self.assertEqual(self.client.post(f"/admin/requests/{third}/move", json={"direction": "up"}, headers=ADMIN).status_code, 200)
        self.assertEqual(self.pending_uris(), ["first", "third", "second"])
        self.assertEqual(self.client.post(f"/admin/requests/{first}/move", json={"direction": "up"}, headers=ADMIN).status_code, 409)

        storage.init_db()
        self.assertEqual(self.pending_uris(), ["first", "third", "second"])
        self.assertEqual(self.client.post("/admin/requests/999/move", json={"direction": "down"}, headers=ADMIN).status_code, 404)

    def test_delete_restore_append_and_reject_banned_song(self):
        first = self.make_pending("first")
        second = self.make_pending("second")
        self.assertEqual(self.client.delete(f"/admin/requests/{first}", headers=ADMIN).status_code, 200)
        self.assertEqual(self.pending_uris(), ["second"])
        deleted = self.client.get("/admin/requests/deleted", headers=ADMIN)
        self.assertEqual([row["id"] for row in deleted.json()], [first])
        self.assertEqual(self.client.post(f"/admin/requests/{first}/restore", headers=ADMIN).status_code, 200)
        self.assertEqual(self.pending_uris(), ["second", "first"])

        self.assertEqual(self.client.delete(f"/admin/requests/{first}", headers=ADMIN).status_code, 200)
        self.client.post("/admin/song-bans", json={"songid": "spotify:track:first"}, headers=ADMIN)
        self.assertEqual(self.client.post(f"/admin/requests/{first}/restore", headers=ADMIN).status_code, 409)
        self.assertEqual(self.client.delete(f"/admin/requests/{second}", headers=ADMIN).status_code, 200)
        self.assertEqual(self.client.delete(f"/admin/requests/{second}", headers=ADMIN).status_code, 409)

    def test_approve_only_pending_and_does_not_queue_twice(self):
        request_id = self.make_pending("track-once")
        self.assertEqual(self.client.post(f"/admin/requests/{request_id}/approve", headers=ADMIN).status_code, 200)
        self.assertEqual(self.client.post(f"/admin/requests/{request_id}/approve", headers=ADMIN).status_code, 409)
        self.add_to_queue.assert_awaited_once_with("track-once")

    def test_moderation_status_and_missing_errors(self):
        request_id = self.make_pending("status-check")
        self.assertEqual(self.client.delete(f"/admin/requests/{request_id}", headers=ADMIN).status_code, 200)
        self.assertEqual(self.client.post(f"/admin/requests/{request_id}/approve", headers=ADMIN).status_code, 409)
        self.add_to_queue.assert_not_awaited()
        self.assertEqual(self.client.delete("/admin/requests/999", headers=ADMIN).status_code, 404)
        self.assertEqual(self.client.post("/admin/requests/999/restore", headers=ADMIN).status_code, 404)

        # An approved request cannot be reordered or archived through the pending controls.
        self.set_auto_approve(True)
        self.assertEqual(self.post_request(uri="already-approved").status_code, 200)
        approved = self.db_rows("SELECT id FROM requests WHERE spotify_uri='already-approved'")[0]["id"]
        self.assertEqual(self.client.post(f"/admin/requests/{approved}/move", json={"direction": "up"}, headers=ADMIN).status_code, 409)
        self.assertEqual(self.client.delete(f"/admin/requests/{approved}", headers=ADMIN).status_code, 409)
        self.assertEqual(self.client.post(f"/admin/requests/{request_id}/move", json={"direction": "sideways"}, headers=ADMIN).status_code, 422)

    def test_concurrent_pending_insertions_get_distinct_ranks(self):
        values = [
            (f"concurrent-{index}", "Song", "Artist", "Album", "", "Host", "127.0.0.1", "pending")
            for index in range(8)
        ]
        with ThreadPoolExecutor(max_workers=8) as executor:
            list(executor.map(storage.insert_request, values))
        rows = self.db_rows("SELECT queue_order FROM requests WHERE status='pending' ORDER BY queue_order")
        ranks = [row["queue_order"] for row in rows]
        self.assertEqual(ranks, list(range(8)))

    def test_migrates_existing_pending_rows_in_submission_order(self):
        conn = sqlite3.connect(self.db_path)
        conn.execute("ALTER TABLE requests DROP COLUMN queue_order")
        conn.execute("INSERT INTO requests (spotify_uri, status, created_at) VALUES ('older', 'pending', '2025-01-01 00:00:00')")
        conn.execute("INSERT INTO requests (spotify_uri, status, created_at) VALUES ('newer', 'pending', '2025-01-02 00:00:00')")
        conn.commit()
        conn.close()

        storage.init_db()
        self.assertEqual(self.pending_uris(), ["older", "newer"])
        self.set_auto_approve(False)
        self.assertEqual(self.post_request(uri="another").status_code, 200)
        self.assertEqual(self.pending_uris(), ["older", "newer", "another"])

