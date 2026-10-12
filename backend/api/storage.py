import ipaddress
import sqlite3
import time
from typing import Dict, NamedTuple, Set, List, Optional, Union

import os

DB_FILE = os.environ.get("DB_FILE", "/data/music_sync.db")

IPNetwork = Union[ipaddress.IPv4Network, ipaddress.IPv6Network]

# In-memory ephemeral state (protected by asyncio single-thread guarantee)
listeners: Dict[str, dict] = {}          # {uuid: {name, ip, last_seen, user_agent, display, theme, effects}}
vote_skips: Dict[str, Set[str]] = {}     # {song_id: {uuid:ip, ...}}
rate_limits: Dict[str, List[float]] = {} # {action:ip: [timestamp, ...]}
rate_limit_windows: Dict[str, int] = {}  # {action:ip: longest window (s) used with it}
submission_ids: Dict[str, float] = {}    # {submission_id: created_at}  TTL 5 min
SUBMISSION_TTL_SECS = 300
play_history: List[dict] = []            # [{...song metadata, played_at}, ...] newest first
upcoming_queue: List[dict] = []          # zero or one next-track metadata item from go-librespot

HISTORY_MAX_ITEMS = 10
HISTORY_TTL_SECS = 3600  # songs drop off the history 1 hour after they played


def active_listener_count(listeners_state: Optional[Dict[str, dict]] = None) -> int:
    """People listening, excluding display/kiosk screens (index.html?display).

    Kiosks stay in the registry so the admin panel can still see and control
    them, but they are nobody in the room — they must not inflate the public
    count or the skip-vote threshold derived from it.
    """
    state = listeners if listeners_state is None else listeners_state
    return sum(1 for d in state.values() if not d.get("display"))


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


def remove_from_history(songid: str) -> bool:
    """Remove the newest history entry for ``songid``.

    Used when a paused track resumes so it leaves history and becomes current
    again. Returns True if an entry was removed.
    """
    if not songid:
        return False
    for i, entry in enumerate(play_history):
        if entry.get("songid") == songid:
            del play_history[i]
            return True
    return False


def get_history() -> List[dict]:
    """Return the current, pruned play history (newest first)."""
    prune_history()
    return list(play_history)


def init_db():
    conn = sqlite3.connect(DB_FILE)
    # WAL lets the thread-pool readers run alongside a writer instead of blocking.
    # The mode is stored in the file, so an existing database switches on first start.
    conn.execute("PRAGMA journal_mode=WAL")
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
    # Created IF NOT EXISTS so existing databases get it too. Serves the admin list
    # (newest first) and the 12-hour cleanup; the table is small, so nothing else is indexed.
    conn.execute("CREATE INDEX IF NOT EXISTS idx_requests_created_at ON requests(created_at)")
    conn.execute("INSERT OR IGNORE INTO settings VALUES ('auto_approve', 'true')")
    conn.commit()
    conn.close()


def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    # Wait up to 5 s for a lock held by another connection (worker threads share the file).
    conn.execute("PRAGMA busy_timeout=5000")
    # Full durability: a committed request or ban survives power loss. Explicit so it
    # does not depend on the SQLite build's default.
    conn.execute("PRAGMA synchronous=FULL")
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


class Ban(NamedTuple):
    """An active ban row with its network parsed once (None if not a valid IP/CIDR)."""
    ip: str
    uuid: str
    network: Optional[IPNetwork]


def _parse_network(value: str) -> Optional[IPNetwork]:
    try:
        return ipaddress.ip_network(value, strict=False)
    except ValueError:
        return None


def load_active_bans(conn: sqlite3.Connection) -> List[Ban]:
    """All bans that match right now (expires_at NULL means permanent)."""
    rows = conn.execute(
        """SELECT ip, uuid FROM bans
           WHERE expires_at IS NULL OR expires_at > datetime('now')"""
    ).fetchall()
    return [
        Ban(row["ip"] or "", row["uuid"] or "",
            _parse_network(row["ip"]) if row["ip"] else None)
        for row in rows
    ]


def ban_matches(bans: List[Ban], ip: Optional[str], uuid: Optional[str] = None) -> bool:
    """True if either the IP or the listener UUID matches one of ``bans``.

    A ban row can carry an IP (or CIDR network), a UUID, or both — matching on
    either identifier is enough (so a banned device is caught even behind a
    new IP, and vice versa). IP matching is containment-aware: a request from
    any address inside a banned network matches, which is what catches IPv6
    users hopping around inside their /64 (bans are stored at /64 granularity
    by normalize_ban_ip). Unparseable ban entries can only exact-match.
    """
    uuid = (uuid or "").strip()
    ip = (ip or "").strip()
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        addr = None

    for ban in bans:
        if uuid and ban.uuid and ban.uuid == uuid:
            return True
        if not ban.ip or not ip:
            continue
        if ban.ip == ip:
            return True  # exact match (also covers unparseable entries)
        if addr is not None and ban.network is not None and addr in ban.network:
            return True
    return False


def is_banned(ip: Optional[str], uuid: Optional[str] = None) -> bool:
    """True if either the IP or the listener UUID has an active ban."""
    conn = get_db()
    try:
        bans = load_active_bans(conn)
    finally:
        conn.close()
    return ban_matches(bans, ip, uuid)


def normalize_songid(songid: Optional[str]) -> str:
    """Reduce a Spotify track reference to its bare id.

    Requests arrive as bare ids from the frontend, but tolerate the
    'spotify:track:<id>' URI form too.
    """
    return (songid or "").strip().split(":")[-1]


def load_song_bans(conn: sqlite3.Connection) -> Set[str]:
    """The set of banned song ids (bare Spotify track ids)."""
    return {row["songid"] for row in conn.execute("SELECT songid FROM song_bans").fetchall()}


def is_song_banned_in(song_bans: Set[str], songid: Optional[str]) -> bool:
    sid = normalize_songid(songid)
    return bool(sid) and sid in song_bans


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


# ─── Database operations ─────────────────────────────────────────────────────
# Each function is one complete operation (connect, queries, commit, close). The API
# runs them in worker threads with asyncio.to_thread, so a connection is never shared
# across threads or left open across an await on the network.

def read_auto_approve() -> Optional[str]:
    """The raw auto_approve setting, or None if it has never been stored."""
    conn = get_db()
    try:
        row = conn.execute("SELECT value FROM settings WHERE key='auto_approve'").fetchone()
        return row["value"] if row else None
    finally:
        conn.close()


def write_auto_approve(value: str) -> None:
    conn = get_db()
    try:
        conn.execute("INSERT OR REPLACE INTO settings VALUES ('auto_approve', ?)", (value,))
        conn.commit()
    finally:
        conn.close()


def get_request(request_id: int) -> Optional[dict]:
    conn = get_db()
    try:
        row = conn.execute("SELECT * FROM requests WHERE id=?", (request_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def set_request_status(request_id: int, status: str) -> None:
    conn = get_db()
    try:
        conn.execute("UPDATE requests SET status=? WHERE id=?", (status, request_id))
        conn.commit()
    finally:
        conn.close()


def insert_request(values: tuple) -> None:
    """Insert a request row; ``values`` follows the column order of the INSERT below."""
    conn = get_db()
    try:
        conn.execute(
            """INSERT INTO requests
               (spotify_uri, track_name, artist_name, album_name, cover_url,
                requester_name, requester_ip, status)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            values,
        )
        conn.commit()
    finally:
        conn.close()


def list_admin_requests(limit: int = 100) -> List[dict]:
    """Recent pending/approved requests with requester-ban and song-ban flags.

    Bans are loaded once for the whole list, not once per row.
    """
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT * FROM requests WHERE status IN ('pending', 'approved') ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        bans = load_active_bans(conn)
        song_bans = load_song_bans(conn)
    finally:
        conn.close()
    result = []
    for r in rows:
        d = dict(r)
        d["requester_banned"] = ban_matches(bans, d.get("requester_ip"))
        d["song_banned"] = is_song_banned_in(song_bans, d.get("spotify_uri"))
        result.append(d)
    return result


def active_bans() -> List[Ban]:
    conn = get_db()
    try:
        return load_active_bans(conn)
    finally:
        conn.close()


def list_bans() -> List[dict]:
    """All bans, after sweeping expired temporary bans."""
    conn = get_db()
    try:
        # Expired temporary bans are dead weight — sweep them while we're here.
        conn.execute("DELETE FROM bans WHERE expires_at IS NOT NULL AND expires_at <= datetime('now')")
        conn.commit()
        rows = conn.execute("SELECT * FROM bans ORDER BY created_at DESC").fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def add_ban(ip: str, uuid: str, reason: str, minutes: int) -> None:
    conn = get_db()
    try:
        if minutes > 0:
            conn.execute(
                """INSERT INTO bans (ip, uuid, reason, expires_at)
                   VALUES (?, ?, ?, datetime('now', '+' || ? || ' minutes'))""",
                (ip, uuid, reason, minutes),
            )
        else:
            conn.execute(
                "INSERT INTO bans (ip, uuid, reason) VALUES (?, ?, ?)",
                (ip, uuid, reason),
            )
        conn.commit()
    finally:
        conn.close()


def delete_ban(ban_id: int) -> None:
    conn = get_db()
    try:
        conn.execute("DELETE FROM bans WHERE id=?", (ban_id,))
        conn.commit()
    finally:
        conn.close()


def list_song_bans() -> List[dict]:
    conn = get_db()
    try:
        rows = conn.execute("SELECT * FROM song_bans ORDER BY created_at DESC").fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def add_song_ban(sid: str, track_name: str, artist_name: str) -> None:
    conn = get_db()
    try:
        conn.execute(
            "INSERT OR IGNORE INTO song_bans (songid, track_name, artist_name) VALUES (?, ?, ?)",
            (sid, track_name, artist_name),
        )
        # A banned song has no business sitting in the pending queue either
        conn.execute(
            "UPDATE requests SET status='deleted' WHERE status='pending' AND spotify_uri IN (?, ?)",
            (sid, f"spotify:track:{sid}"),
        )
        conn.commit()
    finally:
        conn.close()


def delete_song_ban(ban_id: int) -> None:
    conn = get_db()
    try:
        conn.execute("DELETE FROM song_bans WHERE id=?", (ban_id,))
        conn.commit()
    finally:
        conn.close()


def check_rate_limit(action: str, ip: str, limit: int, window_secs: int) -> bool:
    key = f"{action}:{ip}"
    now = time.time()
    # Pruning uses the longest window any caller has used for this key, so an
    # entry is only dropped once no caller could still be counting it.
    rate_limit_windows[key] = max(rate_limit_windows.get(key, 0), window_secs)
    times = [t for t in rate_limits.get(key, []) if now - t < window_secs]
    rate_limits[key] = times
    if len(times) >= limit:
        return False
    rate_limits[key].append(now)
    return True


def prune_request_state() -> None:
    """Drop rate-limit and submission entries that can no longer affect a check."""
    now = time.time()
    for key in list(rate_limits):
        window = rate_limit_windows.get(key, 0)
        times = [t for t in rate_limits[key] if now - t < window]
        if times:
            rate_limits[key] = times
        else:
            del rate_limits[key]
            rate_limit_windows.pop(key, None)
    for sid in [k for k, v in submission_ids.items() if now - v > SUBMISSION_TTL_SECS]:
        del submission_ids[sid]


def cleanup_old_requests():
    """Delete requests older than 12 hours."""
    conn = get_db()
    try:
        conn.execute(
            "DELETE FROM requests WHERE created_at < datetime('now', '-12 hours')"
        )
        conn.commit()
    finally:
        conn.close()


def check_submission_id(submission_id: str) -> bool:
    """Returns True if this is a new submission (not duplicate), False if already seen."""
    now = time.time()
    # Cleanup expired entries (TTL: 5 minutes)
    expired = [k for k, v in list(submission_ids.items()) if now - v > SUBMISSION_TTL_SECS]
    for k in expired:
        del submission_ids[k]
    if submission_id in submission_ids:
        return False
    submission_ids[submission_id] = now
    return True
