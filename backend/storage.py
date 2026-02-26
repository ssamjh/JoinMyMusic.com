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
    conn.execute("INSERT OR IGNORE INTO settings VALUES ('auto_approve', 'true')")
    conn.commit()
    conn.close()


def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn


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
