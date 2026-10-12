"""In-memory request state: rate limits and submission ids, including pruning."""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import support  # noqa: E402
from support import ApiTestCase, StateTestCase, storage  # noqa: E402


class RateLimitTests(StateTestCase):
    def test_limit_is_enforced_within_the_window(self):
        results = [storage.check_rate_limit("vote", "1.2.3.4", 5, 60) for _ in range(6)]
        self.assertEqual(results, [True] * 5 + [False])

    def test_prune_drops_keys_with_no_recent_hits(self):
        storage.check_rate_limit("vote", "1.2.3.4", 5, 60)
        storage.rate_limits["vote:1.2.3.4"] = [time.time() - 61]
        storage.prune_request_state()
        self.assertNotIn("vote:1.2.3.4", storage.rate_limits)
        self.assertNotIn("vote:1.2.3.4", storage.rate_limit_windows)
        # A later check behaves exactly as for a fresh key.
        self.assertTrue(storage.check_rate_limit("vote", "1.2.3.4", 5, 60))

    def test_prune_keeps_recent_hits_and_still_enforces_the_limit(self):
        for _ in range(10):
            storage.check_rate_limit("add", "1.2.3.4", 10, 1800)
        storage.prune_request_state()
        self.assertEqual(len(storage.rate_limits["add:1.2.3.4"]), 10)
        self.assertFalse(storage.check_rate_limit("add", "1.2.3.4", 10, 1800))

    def test_prune_uses_the_longest_window_seen_for_a_key(self):
        # One caller uses a 1800 s window; a shorter-window caller shares the key.
        storage.check_rate_limit("add", "1.2.3.4", 10, 1800)
        storage.check_rate_limit("add", "1.2.3.4", 10, 60)
        storage.rate_limits["add:1.2.3.4"] = [time.time() - 100]  # older than 60 s, not 1800 s
        storage.prune_request_state()
        self.assertEqual(len(storage.rate_limits["add:1.2.3.4"]), 1)

    def test_prune_removes_only_expired_hits_from_a_mixed_key(self):
        now = time.time()
        storage.rate_limit_windows["vote:1.2.3.4"] = 60
        storage.rate_limits["vote:1.2.3.4"] = [now - 90, now - 10]
        storage.prune_request_state()
        self.assertEqual(storage.rate_limits["vote:1.2.3.4"], [now - 10])


class SubmissionIdTests(StateTestCase):
    def test_duplicate_submission_is_rejected(self):
        self.assertTrue(storage.check_submission_id("abc"))
        self.assertFalse(storage.check_submission_id("abc"))

    def test_prune_expires_submission_ids_after_five_minutes(self):
        now = time.time()
        storage.submission_ids["old"] = now - 301
        storage.submission_ids["fresh"] = now - 299
        storage.prune_request_state()
        self.assertEqual(set(storage.submission_ids), {"fresh"})
        self.assertTrue(storage.check_submission_id("old"))


class DatabaseMaintenanceTests(ApiTestCase):
    def test_init_db_enables_wal_and_the_created_at_index(self):
        conn = storage.get_db()
        try:
            self.assertEqual(conn.execute("PRAGMA journal_mode").fetchone()[0], "wal")
            indexes = {r["name"] for r in conn.execute("PRAGMA index_list(requests)").fetchall()}
            self.assertIn("idx_requests_created_at", indexes)
            self.assertEqual(conn.execute("PRAGMA busy_timeout").fetchone()[0], 5000)
        finally:
            conn.close()
        storage.init_db()  # idempotent on an existing database

    def test_twelve_hour_cleanup_removes_only_old_requests(self):
        conn = storage.get_db()
        try:
            conn.execute(
                "INSERT INTO requests (spotify_uri, created_at) VALUES ('old', datetime('now', '-13 hours'))"
            )
            conn.execute(
                "INSERT INTO requests (spotify_uri, created_at) VALUES ('new', datetime('now', '-1 hours'))"
            )
            conn.commit()
        finally:
            conn.close()
        storage.cleanup_old_requests()
        self.assertEqual([r["spotify_uri"] for r in self.db_rows("SELECT spotify_uri FROM requests")], ["new"])


if __name__ == "__main__":
    support.unittest.main()
