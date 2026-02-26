import asyncio
import json
import logging
import re
import time
from contextlib import asynccontextmanager
from typing import Optional

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from pydantic import BaseModel

import os

AUTH_KEY = os.environ.get("AUTH_KEY", "change_me")
TURNSTILE_SECRET = os.environ.get("TURNSTILE_SECRET", "")
from spotify import SpotifyClient
from sse import broadcaster, cleanup_listeners, cleanup_old_requests, poll_spotify, refresh_token_loop
from storage import (
    check_rate_limit,
    check_submission_id,
    get_db,
    init_db,
    listeners,
    submission_ids,
    vote_skips,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

spotify_client = SpotifyClient()


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    asyncio.create_task(poll_spotify(spotify_client, listeners, vote_skips))
    asyncio.create_task(cleanup_listeners(listeners))
    asyncio.create_task(refresh_token_loop(spotify_client))
    asyncio.create_task(cleanup_old_requests())
    yield


app = FastAPI(lifespan=lifespan)

# ─── Auth ────────────────────────────────────────────────────────────────────

def require_admin(request: Request):
    key = request.headers.get("X-Auth-Key") or request.query_params.get("key")
    if key != AUTH_KEY:
        raise HTTPException(status_code=403, detail="Forbidden")

# ─── Turnstile ───────────────────────────────────────────────────────────────

async def verify_turnstile(token: str, ip: str) -> bool:
    if not TURNSTILE_SECRET:
        return True  # Skip verification when no secret configured
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            "https://challenges.cloudflare.com/turnstile/v0/siteverify",
            data={"secret": TURNSTILE_SECRET, "response": token, "remoteip": ip},
        )
        return resp.json().get("success", False)


async def verify_spotify_token(token: str) -> str:
    """Verify a Spotify access token and return the user's display name.
    Raises HTTPException if the token is invalid."""
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            "https://api.spotify.com/v1/me",
            headers={"Authorization": f"Bearer {token}"},
        )
    if resp.status_code != 200:
        raise HTTPException(status_code=401, detail="Invalid or expired Spotify token. Please sign in again.")
    data = resp.json()
    return data.get("display_name") or data.get("id") or "Unknown"

# ─── SSE ─────────────────────────────────────────────────────────────────────

@app.get("/api/events")
async def sse_events(request: Request):
    async def event_generator():
        # Subscribe before sending initial state to avoid missing events
        queue = broadcaster.subscribe()
        try:
            # Send current state immediately on connect
            metadata = await spotify_client.get_current_playback()
            yield f"event: metadata\ndata: {json.dumps(metadata.get('current', {}))}\n\n"
            yield f"event: listeners\ndata: {json.dumps({'count': len(listeners)})}\n\n"

            while True:
                try:
                    data = await asyncio.wait_for(queue.get(), timeout=20)
                except asyncio.TimeoutError:
                    data = ": keepalive\n\n"
                yield data
        except asyncio.CancelledError:
            raise
        finally:
            broadcaster.unsubscribe(queue)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )

# ─── Metadata (REST fallback) ─────────────────────────────────────────────────

@app.get("/api/metadata")
async def get_metadata():
    return await spotify_client.get_current_playback()

# ─── Auth status ─────────────────────────────────────────────────────────────

@app.get("/api/auth_status")
async def auth_status():
    return {"authenticated": spotify_client.is_authenticated()}

# ─── Listener ────────────────────────────────────────────────────────────────

UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")


def sanitize_name(name: Optional[str]) -> Optional[str]:
    if not name:
        return None
    import html as html_mod
    name = re.sub(r"[\x00-\x1F\x7F-\xFF]", "", name)
    name = re.sub(r"\s+", " ", name).strip()
    name = html_mod.escape(name)
    return name[:50] or None


class ListenerPayload(BaseModel):
    uuid: str
    name: Optional[str] = None
    volume: Optional[int] = None  # 0–100


@app.post("/api/listener", status_code=204)
async def register_listener(payload: ListenerPayload, request: Request):
    if not UUID_RE.match(payload.uuid):
        raise HTTPException(status_code=400, detail="Invalid UUID")
    volume = max(0, min(100, payload.volume)) if payload.volume is not None else None
    listeners[payload.uuid] = {
        "name": sanitize_name(payload.name),
        "ip": request.client.host,
        "last_seen": time.time(),
        "user_agent": request.headers.get("user-agent", "Unknown"),
        "volume": volume,
        "uuid": payload.uuid,
    }


@app.get("/api/listener/stats")
async def listener_stats():
    return {"count": len(listeners)}

# ─── Request ─────────────────────────────────────────────────────────────────

class RequestPayload(BaseModel):
    uri: str
    name: str
    submission_id: str
    turnstile: str


@app.post("/api/request")
async def add_request(payload: RequestPayload, request: Request):
    ip = request.client.host

    if not check_rate_limit("add", ip, 10, 1800):
        raise HTTPException(status_code=429, detail="Slow down on the requests there bud. Try again soon.")
    if not payload.uri:
        raise HTTPException(status_code=400, detail="No URI provided")
    if not payload.name:
        raise HTTPException(status_code=400, detail="Please provide your name")

    if not await verify_turnstile(payload.turnstile, ip):
        raise HTTPException(status_code=400, detail="Challenge verification failed")

    if not check_submission_id(payload.submission_id):
        return {"success": True, "message": "Your request was already received!"}

    name = sanitize_name(payload.name)

    # Fetch track info (best-effort; don't fail the request if unavailable)
    track_info: dict = {}
    try:
        track_info = await spotify_client.get_track_info(payload.uri)
    except Exception:
        pass

    db = get_db()
    try:
        row = db.execute("SELECT value FROM settings WHERE key='auto_approve'").fetchone()
        auto_approve = row["value"] == "true" if row else False

        if auto_approve:
            await spotify_client.add_to_queue(payload.uri)
            status = "approved"
            message = "Your request has been automatically approved and added to the queue!"
        else:
            status = "pending"
            message = "Thanks, your request has been added to the queue for approval!"

        db.execute(
            """INSERT INTO requests
               (spotify_uri, track_name, artist_name, album_name, cover_url,
                requester_name, requester_ip, status)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                payload.uri,
                track_info.get("song", ""),
                ", ".join(a["name"] for a in track_info.get("artist", [])),
                track_info.get("album", ""),
                track_info.get("cover", ""),
                name,
                ip,
                status,
            ),
        )
        db.commit()
    finally:
        db.close()

    return {"success": True, "message": message}

# ─── Vote Skip ───────────────────────────────────────────────────────────────

class SkipPayload(BaseModel):
    uuid: str
    songid: str
    turnstile: str


@app.post("/api/skip")
async def vote_skip(payload: SkipPayload, request: Request):
    ip = request.client.host

    if not await verify_turnstile(payload.turnstile, ip):
        raise HTTPException(status_code=400, detail="Turnstile verification failed")

    if payload.uuid not in listeners:
        raise HTTPException(status_code=400, detail="Invalid or inactive listener")

    if not check_rate_limit("vote", ip, 5, 60):
        raise HTTPException(status_code=429, detail="Too many vote attempts. Please wait before trying again.")

    metadata = await spotify_client.get_current_playback()
    current_song_id = metadata.get("current", {}).get("songid", "")
    if payload.songid != current_song_id:
        raise HTTPException(status_code=400, detail="Voted song does not match currently playing song")

    song_id = payload.songid
    if song_id not in vote_skips:
        vote_skips[song_id] = set()

    combined = f"{payload.uuid}:{ip}"
    if combined in vote_skips[song_id]:
        raise HTTPException(status_code=400, detail="You have already voted to skip this song")

    vote_skips[song_id].add(combined)
    count = len(vote_skips[song_id])
    total_listeners = len(listeners)
    needed = max(2, -(-total_listeners // 2))  # ceil division

    if count >= needed:
        try:
            await spotify_client.skip()
            vote_skips.pop(song_id, None)
            await broadcaster.broadcast("skipvotes", {"song": song_id, "count": 0, "needed": needed})
            return {"success": True, "message": "Song skipped successfully"}
        except Exception as e:
            raise HTTPException(status_code=500, detail="Failed to skip the song")

    await broadcaster.broadcast("skipvotes", {"song": song_id, "count": count, "needed": needed})
    return {"success": True, "message": "Vote recorded", "count": count, "needed": needed}


@app.get("/api/skip/stats")
async def skip_stats():
    metadata = await spotify_client.get_current_playback()
    song_id = metadata.get("current", {}).get("songid", "")
    count = len(vote_skips.get(song_id, set()))
    total_listeners = len(listeners)
    needed = max(2, -(-total_listeners // 2))
    return {"song": song_id, "count": count, "needed": needed}

# ─── Spotify OAuth ───────────────────────────────────────────────────────────

@app.get("/api/setup")
async def setup():
    return RedirectResponse(spotify_client.get_auth_url())


@app.get("/api/callback")
async def callback(code: Optional[str] = None):
    if code and spotify_client.handle_callback(code):
        message = "Authentication successful! This window will close in 10 seconds."
    else:
        message = "Error during authentication."
    return HTMLResponse(f"""
    <html>
    <head>
        <title>Spotify Callback</title>
        <script>setTimeout(function(){{ window.close(); }}, 10000);</script>
    </head>
    <body><p>{message}</p></body>
    </html>
    """)

# ─── Admin ───────────────────────────────────────────────────────────────────

ADMIN_HTML = """<!DOCTYPE html>
<html lang="en" data-bs-theme="auto">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Admin &mdash; Music Sync</title>
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.2/dist/css/bootstrap.min.css" rel="stylesheet" crossorigin="anonymous">
    <style>
        body { font-size: 0.9rem; }
        .album-art { width: 56px; height: 56px; object-fit: cover; border-radius: 6px; flex-shrink: 0; }
        .album-art-placeholder { width: 56px; height: 56px; border-radius: 6px; flex-shrink: 0; }
        .card { margin-bottom: 0.6rem; }
        .card-body { padding: 0.6rem 0.75rem; }
        .track-title { font-weight: 600; margin-bottom: 0.1rem; }
        .track-meta { color: var(--bs-secondary-color); margin-bottom: 0.1rem; }
        .listener-name { font-weight: 600; }
        .volume-bar { height: 6px; border-radius: 3px; background: var(--bs-border-color); overflow: hidden; }
        .volume-fill { height: 100%; background: #0d6efd; border-radius: 3px; transition: width 0.3s; }
        .section-header { font-size: 1rem; font-weight: 600; text-transform: uppercase;
                          letter-spacing: 0.05em; color: var(--bs-secondary-color);
                          margin: 1rem 0 0.5rem; border-bottom: 1px solid var(--bs-border-color); padding-bottom: 0.25rem; }
    </style>
</head>
<body>
<div class="container-fluid py-3" style="max-width:1200px">
    <div class="d-flex align-items-center justify-content-between mb-3">
        <h4 class="mb-0">Music Sync Admin</h4>
        <div class="form-check form-switch mb-0">
            <input class="form-check-input" type="checkbox" id="autoApproveToggle">
            <label class="form-check-label" for="autoApproveToggle">Auto-approve</label>
        </div>
    </div>

    <div class="row g-3">
        <!-- Requests column -->
        <div class="col-lg-8">
            <div class="section-header">Pending Requests <span id="pending-count" class="badge bg-warning text-dark ms-1">0</span></div>
            <div id="pending-list"></div>

            <div class="section-header mt-3">Approved Requests <span id="approved-count" class="badge bg-success ms-1">0</span></div>
            <div id="approved-list"></div>
        </div>

        <!-- Listeners column -->
        <div class="col-lg-4">
            <div class="section-header">Active Listeners <span id="listener-count" class="badge bg-primary ms-1">0</span></div>
            <div id="listener-list"></div>
        </div>
    </div>
</div>

<script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.2/dist/js/bootstrap.bundle.min.js" crossorigin="anonymous"></script>
<script>
    const authKey = new URLSearchParams(window.location.search).get('key') || '';

    function esc(s) {
        return String(s || '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
    }

    function renderRequests(requests, pending) {
        if (requests.length === 0) return '<p class="text-secondary">None.</p>';
        return requests.map(r => `
            <div class="card ${pending ? '' : 'border-success'}">
                <div class="card-body d-flex gap-2 align-items-center">
                    ${r.cover_url
                        ? `<img src="${esc(r.cover_url)}" class="album-art">`
                        : `<div class="album-art-placeholder bg-secondary-subtle"></div>`}
                    <div class="flex-grow-1 overflow-hidden">
                        <div class="track-title text-truncate">${esc(r.track_name || r.spotify_uri)}</div>
                        <div class="track-meta text-truncate">${esc(r.artist_name || '')}${r.album_name ? ' &mdash; ' + esc(r.album_name) : ''}</div>
                        <div class="track-meta">From <strong>${esc(r.requester_name || 'Anonymous')}</strong> &middot; ${esc(r.requester_ip || '')} &middot; ${r.created_at}</div>
                    </div>
                    ${pending ? `
                    <div class="d-flex flex-column gap-1">
                        <button class="btn btn-success btn-sm" onclick="approveRequest(${r.id})">&#10003;</button>
                        <button class="btn btn-danger btn-sm" onclick="deleteRequest(${r.id})">&#10005;</button>
                    </div>` : ''}
                </div>
            </div>`).join('');
    }

    function renderListeners(listenersData) {
        if (listenersData.length === 0) return '<p class="text-secondary">No active listeners.</p>';
        return listenersData.map(l => {
            const vol = l.volume != null ? l.volume : null;
            const skipBadge = l.voted_skip
                ? '<span class="badge bg-danger ms-1">voted skip</span>'
                : '';
            const volHtml = vol != null ? `
                <div class="d-flex align-items-center gap-2 mt-1">
                    <small class="text-secondary" style="width:3rem">Vol ${vol}%</small>
                    <div class="volume-bar flex-grow-1"><div class="volume-fill" style="width:${vol}%"></div></div>
                </div>` : '';
            return `
            <div class="card">
                <div class="card-body">
                    <div class="listener-name">${esc(l.name || 'Anonymous')} ${skipBadge}</div>
                    <div class="track-meta">${esc(l.ip)} &middot; ${new Date(l.last_seen * 1000).toLocaleTimeString()}</div>
                    <div class="track-meta text-truncate" style="max-width:100%">${esc(l.user_agent)}</div>
                    ${volHtml}
                </div>
            </div>`;
        }).join('');
    }

    async function loadData() {
        const [reqRes, lisRes, settingsRes] = await Promise.all([
            fetch('/api/admin/requests?key=' + authKey),
            fetch('/api/admin/listeners?key=' + authKey),
            fetch('/api/admin/settings?key=' + authKey),
        ]);
        if (!reqRes.ok) {
            document.querySelector('.container-fluid').innerHTML = '<p class="text-danger mt-4">Access denied.</p>';
            return;
        }
        const requests = await reqRes.json();
        const listenersData = await lisRes.json();
        const settings = await settingsRes.json();

        document.getElementById('autoApproveToggle').checked = settings.auto_approve === 'true';

        const pending = requests.filter(r => r.status === 'pending');
        const approved = requests.filter(r => r.status === 'approved');

        document.getElementById('pending-count').textContent = pending.length;
        document.getElementById('approved-count').textContent = approved.length;
        document.getElementById('listener-count').textContent = listenersData.length;

        document.getElementById('pending-list').innerHTML = renderRequests(pending, true);
        document.getElementById('approved-list').innerHTML = renderRequests(approved, false);
        document.getElementById('listener-list').innerHTML = renderListeners(listenersData);
    }

    async function approveRequest(id) {
        const res = await fetch('/api/admin/requests/' + id + '/approve?key=' + authKey, {
            method: 'POST', headers: {'Content-Type': 'application/json'}
        });
        const data = await res.json();
        if (!data.success) alert(data.detail || 'Failed to approve');
        loadData();
    }

    async function deleteRequest(id) {
        await fetch('/api/admin/requests/' + id + '?key=' + authKey, {method: 'DELETE'});
        loadData();
    }

    document.getElementById('autoApproveToggle').addEventListener('change', async function() {
        await fetch('/api/admin/auto-approve?key=' + authKey, {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({enabled: this.checked})
        });
    });

    loadData();
    setInterval(loadData, 10000);
</script>
</body>
</html>"""


@app.get("/api/admin", response_class=HTMLResponse)
async def admin_panel(key: Optional[str] = None):
    if key != AUTH_KEY:
        raise HTTPException(status_code=403, detail="Forbidden")
    return ADMIN_HTML


@app.get("/api/admin/requests")
async def admin_get_requests(_: None = Depends(require_admin)):
    db = get_db()
    try:
        rows = db.execute(
            "SELECT * FROM requests WHERE status IN ('pending', 'approved') ORDER BY created_at DESC LIMIT 100"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        db.close()


@app.post("/api/admin/requests/{request_id}/approve")
async def admin_approve_request(request_id: int, _: None = Depends(require_admin)):
    db = get_db()
    try:
        row = db.execute("SELECT * FROM requests WHERE id=?", (request_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Request not found")
        await spotify_client.add_to_queue(row["spotify_uri"])
        db.execute("UPDATE requests SET status='approved' WHERE id=?", (request_id,))
        db.commit()
        return {"success": True}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        db.close()


@app.delete("/api/admin/requests/{request_id}")
async def admin_delete_request(request_id: int, _: None = Depends(require_admin)):
    db = get_db()
    try:
        db.execute("UPDATE requests SET status='deleted' WHERE id=?", (request_id,))
        db.commit()
        return {"success": True}
    finally:
        db.close()


@app.get("/api/admin/listeners")
async def admin_get_listeners(_: None = Depends(require_admin)):
    # Collect all UUIDs that have voted to skip the current song
    current_voters: set[str] = set()
    for voters in vote_skips.values():
        for entry in voters:
            uuid = entry.split(":")[0]
            current_voters.add(uuid)

    result = []
    for uuid, data in listeners.items():
        result.append({**data, "voted_skip": uuid in current_voters})
    return result


@app.get("/api/admin/settings")
async def admin_get_settings(_: None = Depends(require_admin)):
    db = get_db()
    try:
        row = db.execute("SELECT value FROM settings WHERE key='auto_approve'").fetchone()
        return {"auto_approve": row["value"] if row else "false"}
    finally:
        db.close()


class AutoApprovePayload(BaseModel):
    enabled: bool


@app.post("/api/admin/auto-approve")
async def admin_set_auto_approve(payload: AutoApprovePayload, _: None = Depends(require_admin)):
    db = get_db()
    try:
        value = "true" if payload.enabled else "false"
        db.execute("INSERT OR REPLACE INTO settings VALUES ('auto_approve', ?)", (value,))
        db.commit()
        return {"success": True, "auto_approve": value}
    finally:
        db.close()
