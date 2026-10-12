"""Ban matching: pure matcher, network parsing and expiry, without HTTP."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import support  # noqa: E402
from support import ApiTestCase, storage  # noqa: E402


class BanMatchTests(ApiTestCase):
    def bans(self):
        conn = storage.get_db()
        try:
            return storage.load_active_bans(conn)
        finally:
            conn.close()

    def insert_ban(self, ip="", uuid="", expires_sql=None):
        conn = storage.get_db()
        try:
            conn.execute(
                f"INSERT INTO bans (ip, uuid, expires_at) VALUES (?, ?, {expires_sql or 'NULL'})",
                (ip, uuid),
            )
            conn.commit()
        finally:
            conn.close()

    def test_exact_ip_and_uuid(self):
        self.insert_ban(ip="203.0.113.5", uuid="abc")
        bans = self.bans()
        self.assertTrue(storage.ban_matches(bans, "203.0.113.5"))
        self.assertTrue(storage.ban_matches(bans, "198.51.100.1", "abc"))
        self.assertFalse(storage.ban_matches(bans, "198.51.100.1", "other"))
        self.assertFalse(storage.ban_matches(bans, "", ""))

    def test_ipv4_cidr_containment(self):
        self.insert_ban(ip="10.1.0.0/16")
        bans = self.bans()
        self.assertTrue(storage.ban_matches(bans, "10.1.200.7"))
        self.assertFalse(storage.ban_matches(bans, "10.2.0.1"))

    def test_ipv6_slash64_from_normalized_ban(self):
        stored = storage.normalize_ban_ip("2001:db8:1:2:aaaa:bbbb:cccc:dddd")
        self.assertEqual(stored, "2001:db8:1:2::/64")
        self.insert_ban(ip=stored)
        bans = self.bans()
        self.assertTrue(storage.ban_matches(bans, "2001:db8:1:2:1::5"))
        self.assertFalse(storage.ban_matches(bans, "2001:db8:1:3::5"))

    def test_unparseable_ban_only_exact_matches(self):
        self.insert_ban(ip="not-an-ip")
        bans = self.bans()
        self.assertTrue(storage.ban_matches(bans, "not-an-ip"))
        self.assertFalse(storage.ban_matches(bans, "10.0.0.1"))

    def test_expired_bans_are_not_loaded(self):
        self.insert_ban(ip="203.0.113.9", expires_sql="datetime('now', '-1 minutes')")
        self.insert_ban(ip="203.0.113.10", expires_sql="datetime('now', '+1 hours')")
        self.assertEqual([b.ip for b in self.bans()], ["203.0.113.10"])
        self.assertFalse(storage.is_banned("203.0.113.9"))
        self.assertTrue(storage.is_banned("203.0.113.10"))

    def test_song_ban_set_matches_normalized_ids(self):
        conn = storage.get_db()
        try:
            conn.execute("INSERT INTO song_bans (songid) VALUES ('track1')")
            conn.commit()
            songs = storage.load_song_bans(conn)
        finally:
            conn.close()
        self.assertTrue(storage.is_song_banned_in(songs, "spotify:track:track1"))
        self.assertTrue(storage.is_song_banned_in(songs, "track1"))
        self.assertFalse(storage.is_song_banned_in(songs, "track2"))
        self.assertFalse(storage.is_song_banned_in(songs, ""))


if __name__ == "__main__":
    support.unittest.main()
