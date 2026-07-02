import ipaddress
import sqlite3
import time
from typing import Dict, Set, List, Optional

import os

DB_FILE = os.environ.get("DB_FILE", "/data/music_sync.db")

# In-memory ephemeral state (protected by asyncio single-thread guarantee)
listeners: Dict[str, dict] = {}          # {uuid: {name, ip, last_seen, user_agent}}
vote_skips: Dict[str, Set[str]] = {}     # {song_id: {uuid:ip, ...}}
rate_limits: Dict[str, List[float]] = {} # {action:ip: [timestamp, ...]}
submission_ids: Dict[str, float] = {}    # {submission_id: created_at}  TTL 5 min
play_history: List[dict] = []            # [{...song metadata, played_at}, ...] newest first

HISTORY_MAX_ITEMS = 10
HISTORY_TTL_SECS = 3600  # songs drop off the history 1 hour after they played


def prune_history() -> bool:
    """Drop history entries older than the TTL and trim to the max size.

    Mutates play_history in place. Returns True if anything was removed.
    """
    now = time.time()
    before = len(play_history)
    play_history[:] = [h for h in play_history if now - h["played_at"] < HISTORY_TTL_SECS]
    del play_history[HISTORY_MAX_ITEMS:]
    return len(play_history) != before


def add_to_history(song: dict) -> None:
    """Record a song that just finished playing (newest first), then prune."""
    entry = {k: v for k, v in song.items() if k != "playing"}
    entry["played_at"] = time.time()
    play_history.insert(0, entry)
    prune_history()


def get_history() -> List[dict]:
    """Return the current, pruned play history (newest first)."""
    prune_history()
    return list(play_history)


def init_db():
    conn = sqlite3.connect(DB_FILE)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            spotify_uri TEXT NOT NULL,
            track_name TEXT,
            artist_name TEXT,
            album_name TEXT,
            cover_url TEXT,
            requester_name TEXT,
            requester_ip TEXT,
            status TEXT DEFAULT 'pending',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS bans (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ip TEXT DEFAULT '',
            uuid TEXT DEFAULT '',
            reason TEXT DEFAULT '',
            expires_at TIMESTAMP DEFAULT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS song_bans (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            songid TEXT NOT NULL UNIQUE,
            track_name TEXT DEFAULT '',
            artist_name TEXT DEFAULT '',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("INSERT OR IGNORE INTO settings VALUES ('auto_approve', 'true')")
    conn.commit()
    conn.close()


def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn


def normalize_ban_ip(ip: Optional[str]) -> str:
    """Normalize an IP (or CIDR) for storage in a ban row.

    IPv6 households get at least a whole /64, so banning a single /128
    address is trivially evaded by rotating addresses — collapse any IPv6
    more specific than /64 to its /64 network. IPv4 stays per-address, and
    an explicitly broader CIDR (v4 or v6, e.g. a /56) is kept as entered.
    Unparseable input is stored as-is (it can then only exact-match).
    """
    s = (ip or "").strip()
    if not s:
        return ""
    try:
        net = ipaddress.ip_network(s, strict=False)  # accepts bare IPs and CIDRs
    except ValueError:
        return s
    if net.version == 6 and net.prefixlen > 64:
        net = net.supernet(new_prefix=64)
    if net.prefixlen == net.max_prefixlen:
        return str(net.network_address)
    return str(net)


def is_banned(ip: Optional[str], uuid: Optional[str] = None) -> bool:
    """True if either the IP or the listener UUID has an active ban.

    A ban row can carry an IP (or CIDR network), a UUID, or both — matching on
    either identifier is enough (so a banned device is caught even behind a
    new IP, and vice versa). IP matching is containment-aware: a request from
    any address inside a banned network matches, which is what catches IPv6
    users hopping around inside their /64 (bans are stored at /64 granularity
    by normalize_ban_ip). Temporary bans stop matching once expires_at passes;
    expires_at NULL means permanent.
    """
    conn = get_db()
    try:
        rows = conn.execute(
            """SELECT ip, uuid FROM bans
               WHERE expires_at IS NULL OR expires_at > datetime('now')"""
        ).fetchall()
    finally:
        conn.close()

    uuid = (uuid or "").strip()
    ip = (ip or "").strip()
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        addr = None

    for row in rows:
        if uuid and row["uuid"] and row["uuid"] == uuid:
            return True
        ban_ip = row["ip"]
        if not ban_ip or not ip:
            continue
        if ban_ip == ip:
            return True  # exact match (also covers unparseable entries)
        if addr is not None:
            try:
                if addr in ipaddress.ip_network(ban_ip, strict=False):
                    return True
            except ValueError:
                continue
    return False


def normalize_songid(songid: Optional[str]) -> str:
    """Reduce a Spotify track reference to its bare id.

    Requests arrive as bare ids from the frontend, but tolerate the
    'spotify:track:<id>' URI form too.
    """
    return (songid or "").strip().split(":")[-1]


def is_song_banned(songid: Optional[str]) -> bool:
    sid = normalize_songid(songid)
    if not sid:
        return False
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT 1 FROM song_bans WHERE songid = ? LIMIT 1", (sid,)
        ).fetchone()
        return row is not None
    finally:
        conn.close()


def check_rate_limit(action: str, ip: str, limit: int, window_secs: int) -> bool:
    key = f"{action}:{ip}"
    now = time.time()
    times = [t for t in rate_limits.get(key, []) if now - t < window_secs]
    rate_limits[key] = times
    if len(times) >= limit:
        return False
    rate_limits[key].append(now)
    return True


def cleanup_old_requests():
    """Delete requests older than 12 hours."""
    conn = sqlite3.connect(DB_FILE)
    conn.execute(
        "DELETE FROM requests WHERE created_at < datetime('now', '-12 hours')"
    )
    conn.commit()
    conn.close()


def check_submission_id(submission_id: str) -> bool:
    """Returns True if this is a new submission (not duplicate), False if already seen."""
    now = time.time()
    # Cleanup expired entries (TTL: 5 minutes)
    expired = [k for k, v in list(submission_ids.items()) if now - v > 300]
    for k in expired:
        del submission_ids[k]
    if submission_id in submission_ids:
        return False
    submission_ids[submission_id] = now
    return True
