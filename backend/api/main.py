import asyncio
import json
import logging
import re
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal, Optional

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from pydantic import BaseModel

import os

AUTH_KEY = os.environ.get("AUTH_KEY", "change_me")
TURNSTILE_SECRET = os.environ.get("TURNSTILE_SECRET", "")
# The audio container's control page (meters, bypass, processing graph), reached over the
# Compose network and served to admins under /admin/audio/.
AUDIO_CONTROL_URL = os.environ.get("AUDIO_CONTROL_URL", "http://audio:8090").rstrip("/")
# Comma-separated list of frontend origins allowed to call this API.
ALLOWED_ORIGINS = os.environ.get(
    "ALLOWED_ORIGINS", "https://joinmymusic.com,https://www.joinmymusic.com"
)
from librespot import LibrespotClient
from spotify import COVER_ID_RE, SpotifyClient
from sse import (
    broadcast_both,
    broadcaster,
    cleanup_listeners,
    cleanup_old_requests,
    live_input_state,
    live_metadata_for_clients,
    committed_metadata_for_clients,
    poll_live_input,
    poll_spotify,
    realtime_broadcaster,
    refresh_queue_now,
    refresh_token_loop,
)
from storage import (
    active_listener_count,
    check_rate_limit,
    check_submission_id,
    get_db,
    get_history,
    init_db,
    is_banned,
    is_song_banned,
    normalize_ban_ip,
    normalize_songid,
    listeners,
    submission_ids,
    upcoming_queue,
    vote_skips,
)

logging.basicConfig(level=logging.INFO)
# httpx logs every request at INFO; the playback poll makes one each second.
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

spotify_client = SpotifyClient()
librespot = LibrespotClient(spotify_client)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    tasks = [
        asyncio.create_task(poll_spotify(librespot, listeners, vote_skips)),
        asyncio.create_task(poll_live_input()),
        asyncio.create_task(cleanup_listeners(listeners)),
        asyncio.create_task(refresh_token_loop(spotify_client)),
        asyncio.create_task(cleanup_old_requests()),
    ]
    try:
        yield
    finally:
        # Cancel background loops so SIGTERM/docker stop does not hang on them.
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI(lifespan=lifespan)
app.add_middleware(ProxyHeadersMiddleware, trusted_hosts="*")

# The frontend is static-hosted on its own origin and talks to this API at
# api.joinmymusic.com, so every browser call is cross-origin. No credentials:
# the admin pages are served from this origin and keep using their cookie,
# while the public frontend authenticates with nothing at all.


class CoverExemptCORSMiddleware(CORSMiddleware):
    """CORS for the API, but hands-off for /cover.

    Cover responses are cached for a year, and the same URL is loaded both as a
    plain <img> (no Origin header, so this middleware would add no CORS headers
    at all) and with crossorigin="anonymous" for canvas colour extraction. If
    the middleware reflected the origin, the two would be different cache
    variants of an immutable resource and whichever landed in the browser/edge
    cache first would win — intermittently breaking the CORS one. The route
    instead answers with a constant `Access-Control-Allow-Origin: *`, which is
    correct for every caller and leaves exactly one variant to cache.
    """

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope["path"].startswith("/cover/"):
            return await self.app(scope, receive, send)
        await super().__call__(scope, receive, send)


app.add_middleware(
    CoverExemptCORSMiddleware,
    allow_origins=[o.strip() for o in ALLOWED_ORIGINS.split(",") if o.strip()],
    allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type"],
)

# ─── Auth ────────────────────────────────────────────────────────────────────

def require_admin(request: Request):
    key = (
        request.headers.get("X-Auth-Key")
        or request.query_params.get("key")
        or request.cookies.get("admin_key")
    )
    if key != AUTH_KEY:
        raise HTTPException(status_code=403, detail="Forbidden")


def admin_page_response(request: Request, html: str) -> HTMLResponse:
    """Serve an admin page, remembering a valid ?key= in a cookie so
    navigation between admin pages (and their fetches) stays authenticated
    without carrying the key in every URL."""
    key = request.query_params.get("key")
    has_cookie = request.cookies.get("admin_key") == AUTH_KEY
    if key != AUTH_KEY and not has_cookie:
        raise HTTPException(status_code=403, detail="Forbidden")
    resp = HTMLResponse(html)
    if key == AUTH_KEY and not has_cookie:
        # HttpOnly keeps it away from page JS; Lax stops cross-site POSTs
        # from riding on it while still allowing normal link navigation.
        resp.set_cookie(
            "admin_key", key,
            httponly=True, samesite="lax", max_age=60 * 60 * 24 * 30,
        )
    return resp

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

# ─── SSE ─────────────────────────────────────────────────────────────────────

@app.get("/events")
async def sse_events(request: Request):
    async def event_generator():
        # Subscribe before sending initial state to avoid missing events
        queue = broadcaster.subscribe()
        try:
            # Send current state immediately on connect
            yield f"event: metadata\ndata: {json.dumps(committed_metadata_for_clients())}\n\n"
            yield f"event: listeners\ndata: {json.dumps({'count': active_listener_count()})}\n\n"
            yield f"event: history\ndata: {json.dumps({'history': get_history()})}\n\n"
            yield f"event: queue\ndata: {json.dumps({'queue': upcoming_queue})}\n\n"
            yield f"event: source\ndata: {json.dumps({'live': live_input_state['live']})}\n\n"

            while True:
                try:
                    event, data = await asyncio.wait_for(queue.get(), timeout=20)
                    message = f"event: {event}\ndata: {json.dumps(data)}\n\n"
                except asyncio.TimeoutError:
                    message = ": keepalive\n\n"
                yield message
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

# ─── SSE (realtime, no audio-sync delay) ────────────────────────────────────
# Same event types as /events, but "metadata" is pushed the instant
# Spotify reports a change rather than delayed to line up with the audio
# stream. Filter which categories to receive with query flags, e.g.
# /events-realtime?music=1 for only now-playing/history updates.
# Recognized flags: music (metadata, history, queue, source), listeners, skipvotes, control
# (stop, announce). Omit all flags to receive every category.

_REALTIME_EVENT_CATEGORY = {
    "metadata": "music",
    "history": "music",
    "queue": "music",
    "source": "music",
    "listeners": "listeners",
    "skipvotes": "skipvotes",
    "stop": "control",
    "announce": "control",
    "preferences": "control",
}
_REALTIME_CATEGORIES = ("music", "listeners", "skipvotes", "control")


def _query_flag_truthy(value: Optional[str]) -> bool:
    return value is not None and value.lower() not in ("0", "false", "no", "")


@app.get("/events-realtime")
async def sse_events_realtime(request: Request):
    requested = [c for c in _REALTIME_CATEGORIES if c in request.query_params]
    allowed = (
        {c for c in requested if _query_flag_truthy(request.query_params.get(c))}
        if requested
        else set(_REALTIME_CATEGORIES)
    )

    async def event_generator():
        queue = realtime_broadcaster.subscribe()
        try:
            metadata = await librespot.get_current_playback()
            if "music" in allowed:
                yield f"event: metadata\ndata: {json.dumps(live_metadata_for_clients(metadata.get('current', {})))}\n\n"
                yield f"event: history\ndata: {json.dumps({'history': get_history()})}\n\n"
                yield f"event: queue\ndata: {json.dumps({'queue': upcoming_queue})}\n\n"
                yield f"event: source\ndata: {json.dumps({'live': live_input_state['realtime']})}\n\n"
            if "listeners" in allowed:
                yield f"event: listeners\ndata: {json.dumps({'count': active_listener_count()})}\n\n"

            while True:
                try:
                    event, data = await asyncio.wait_for(queue.get(), timeout=20)
                    if _REALTIME_EVENT_CATEGORY.get(event) not in allowed:
                        continue
                    message = f"event: {event}\ndata: {json.dumps(data)}\n\n"
                except asyncio.TimeoutError:
                    message = ": keepalive\n\n"
                yield message
        except asyncio.CancelledError:
            raise
        finally:
            realtime_broadcaster.unsubscribe(queue)

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

@app.get("/metadata")
async def get_metadata():
    return {"current": committed_metadata_for_clients()}


@app.get("/history")
async def get_play_history():
    return {"history": get_history()}


@app.get("/queue")
async def get_upcoming_queue():
    return {"queue": list(upcoming_queue)}

# ─── Auth status ─────────────────────────────────────────────────────────────

@app.get("/auth_status")
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
    display: bool = False         # display/kiosk screen — not a person in the room
    theme: Optional[Literal["light", "dark"]] = None
    effects: Optional[bool] = None


@app.post("/listener", status_code=204)
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
        "display": payload.display,
        "theme": payload.theme,
        "effects": payload.effects,
    }


@app.get("/listener/stats")
async def listener_stats():
    return {"count": active_listener_count()}

# ─── Search ──────────────────────────────────────────────────────────────────

class SearchPayload(BaseModel):
    query: str
    uuid: Optional[str] = None


BAN_MESSAGE = "You have been banned from requesting songs."


@app.post("/search")
async def search_songs(payload: SearchPayload, request: Request):
    if is_banned(request.client.host, payload.uuid):
        raise HTTPException(status_code=403, detail=BAN_MESSAGE)
    if not check_rate_limit("search", request.client.host, 500, 3600):
        raise HTTPException(status_code=429, detail="Too many searches. Wait a bit and try again.")
    if len(payload.query) < 2:
        raise HTTPException(status_code=400, detail="No search text provided.")
    try:
        results = await spotify_client.search(payload.query)
        return {"results": results}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# ─── Request ─────────────────────────────────────────────────────────────────

class RequestPayload(BaseModel):
    uri: str
    name: str
    submission_id: str
    turnstile: str
    uuid: Optional[str] = None


@app.post("/request")
async def add_request(payload: RequestPayload, request: Request):
    ip = request.client.host

    if is_banned(ip, payload.uuid):
        raise HTTPException(status_code=403, detail=BAN_MESSAGE)
    if is_song_banned(payload.uri):
        raise HTTPException(status_code=403, detail="This song can't be requested.")
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
            await librespot.add_to_queue(payload.uri)
            await refresh_queue_now(librespot)
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


@app.post("/skip")
async def vote_skip(payload: SkipPayload, request: Request):
    ip = request.client.host

    if is_banned(ip, payload.uuid):
        raise HTTPException(status_code=403, detail="You have been banned from voting to skip.")
    if not await verify_turnstile(payload.turnstile, ip):
        raise HTTPException(status_code=400, detail="Turnstile verification failed")

    if payload.uuid not in listeners:
        raise HTTPException(status_code=400, detail="Invalid or inactive listener")

    if not check_rate_limit("vote", ip, 5, 60):
        raise HTTPException(status_code=429, detail="Too many vote attempts. Please wait before trying again.")

    metadata = await librespot.get_current_playback()
    current_song_id =metadata.get("current", {}).get("songid", "")
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
    total_listeners = active_listener_count()
    needed = max(2, -(-total_listeners // 2))  # ceil division

    if count >= needed:
        try:
            await librespot.skip()
            vote_skips.pop(song_id, None)
            await broadcast_both("skipvotes", {"song": song_id, "count": 0, "needed": needed})
            return {"success": True, "message": "Song skipped successfully"}
        except Exception as e:
            raise HTTPException(status_code=500, detail="Failed to skip the song")

    await broadcast_both("skipvotes", {"song": song_id, "count": count, "needed": needed})
    return {"success": True, "message": "Vote recorded", "count": count, "needed": needed}


@app.get("/skip/stats")
async def skip_stats():
    metadata = await librespot.get_current_playback()
    song_id = metadata.get("current", {}).get("songid", "")
    count =len(vote_skips.get(song_id, set()))
    total_listeners = active_listener_count()
    needed = max(2, -(-total_listeners // 2))
    return {"song": song_id, "count": count, "needed": needed}

# ─── Album art ───────────────────────────────────────────────────────────────

@app.get("/cover/{image_id}")
async def cover(image_id: str):
    """Proxy album art so the browser never requests anything from Spotify.

    Art is content-addressed and immutable, so this is cached hard at the edge
    and the origin only ever fetches each image once.
    """
    if not COVER_ID_RE.match(image_id):
        raise HTTPException(status_code=404, detail="Not found")
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(f"https://i.scdn.co/image/{image_id}")
    except httpx.HTTPError:
        raise HTTPException(status_code=502, detail="Upstream image fetch failed")
    if resp.status_code != 200:
        raise HTTPException(status_code=404, detail="Not found")
    return Response(
        content=resp.content,
        media_type=resp.headers.get("content-type", "image/jpeg"),
        headers={
            "Cache-Control": "public, max-age=31536000, immutable",
            # Constant, origin-independent: see CoverExemptCORSMiddleware.
            "Access-Control-Allow-Origin": "*",
        },
    )

# ─── Spotify OAuth ───────────────────────────────────────────────────────────

@app.get("/setup")
async def setup():
    return RedirectResponse(spotify_client.get_auth_url())


@app.get("/callback")
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
<html lang="en" data-bs-theme="dark">
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
        .private-ip { display: inline-block; filter: blur(5px); }
        .private-ip:hover, .private-ip:focus { filter: none; }
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
        <div class="d-flex align-items-center gap-3">
            <button class="btn btn-outline-info btn-sm" onclick="sendMessage('')">Message all</button>
            <button class="btn btn-outline-danger btn-sm" onclick="stopPlayback('')">Stop all</button>
            <a id="bansLink" href="#" class="btn btn-outline-secondary btn-sm">Bans</a>
            <div class="form-check form-switch mb-0">
                <input class="form-check-input" type="checkbox" id="autoApproveToggle">
                <label class="form-check-label" for="autoApproveToggle">Auto-approve</label>
            </div>
        </div>
    </div>

    <!-- Audio monitor: collapsed by default; it loads and streams meters only while shown. -->
    <div class="section-header d-flex align-items-center justify-content-between mt-0">
        <span>Audio</span>
        <button id="audioToggle" type="button" class="btn btn-outline-secondary btn-sm"
                style="text-transform:none;letter-spacing:normal"
                aria-expanded="false" aria-controls="audioMonitor">Show</button>
    </div>
    <div id="audioMonitor" data-jmm-monitor data-embedded data-base="/admin/audio/" hidden></div>

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
    let requestsById = {};

    // Audio monitor (meters, bypasses, processing graph) from the audio container, through
    // the admin proxy. Nothing is fetched until it is first shown, and hiding it stops the
    // meter stream and polling. Its calls carry the admin key like the rest of this page.
    (() => {
        const box = document.getElementById('audioMonitor');
        const toggle = document.getElementById('audioToggle');
        let requested = false;
        toggle.addEventListener('click', () => {
            const show = box.hidden;
            box.hidden = !show;
            toggle.textContent = show ? 'Hide' : 'Show';
            toggle.setAttribute('aria-expanded', String(show));
            if (box.jmmMonitor) {
                if (show) box.jmmMonitor.start(); else box.jmmMonitor.stop();
                return;
            }
            if (!show || requested) return;
            requested = true;
            const q = authKey ? '?key=' + encodeURIComponent(authKey) : '';
            box.dataset.key = authKey;
            const css = document.createElement('link');
            css.rel = 'stylesheet';
            css.href = '/admin/audio/monitor.css' + q;
            document.head.append(css);
            const js = document.createElement('script');
            js.src = '/admin/audio/monitor.js' + q;
            // Hidden again before the script arrived: start, then stop at once.
            js.onload = () => { if (box.hidden && box.jmmMonitor) box.jmmMonitor.stop(); };
            document.body.append(js);
        });
    })();

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
                        <div class="track-title text-truncate">${esc(r.track_name || r.spotify_uri)}
                            ${r.song_banned ? '<span class="badge bg-danger ms-1">song banned</span>' : ''}</div>
                        <div class="track-meta text-truncate">${esc(r.artist_name || '')}${r.album_name ? ' &mdash; ' + esc(r.album_name) : ''}</div>
                        <div class="track-meta">From <strong>${esc(r.requester_name || 'Anonymous')}</strong>
                            ${r.requester_banned ? '<span class="badge bg-danger">banned</span>' : ''}
                            &middot; <span class="private-ip" tabindex="0" aria-label="IP address; hover or focus to reveal">${esc(r.requester_ip || '')}</span> &middot; ${r.created_at}</div>
                    </div>
                    <div class="d-flex flex-column gap-1">
                        ${pending ? `
                        <button class="btn btn-success btn-sm" onclick="approveRequest(${r.id})">&#10003;</button>
                        <button class="btn btn-danger btn-sm" onclick="deleteRequest(${r.id})">&#10005;</button>` : ''}
                        <button class="btn btn-outline-danger btn-sm" title="Ban requester IP" ${r.requester_banned ? 'disabled' : ''} onclick="banUser('${esc(r.requester_ip || '')}', '')">&#128683;</button>
                        <button class="btn btn-outline-warning btn-sm" title="Ban this song" ${r.song_banned ? 'disabled' : ''} onclick="banSong(${r.id})">&#9835;</button>
                    </div>
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
            const bannedBadge = l.banned
                ? '<span class="badge bg-danger ms-1">banned</span>'
                : '';
            // Displays are listed and controllable here but excluded from the
            // public "listening now" count.
            const displayBadge = l.display
                ? '<span class="badge bg-secondary ms-1">display</span>'
                : '';
            const volHtml = vol != null ? `
                <div class="d-flex align-items-center gap-2 mt-1">
                    <small class="text-secondary" style="width:3rem">Vol ${vol}%</small>
                    <div class="volume-bar flex-grow-1"><div class="volume-fill" style="width:${vol}%"></div></div>
                </div>` : '';
            return `
            <div class="card">
                <div class="card-body">
                    <div class="d-flex justify-content-between align-items-start gap-2">
                        <div class="listener-name">${esc(l.name || 'Anonymous')} ${displayBadge} ${skipBadge} ${bannedBadge}</div>
                        <div class="d-flex gap-1">
                            <button class="btn btn-outline-info btn-sm" title="Send popup message to this listener"
                                    onclick="sendMessage('${esc(l.uuid || '')}')">Msg</button>
                            <button class="btn btn-outline-warning btn-sm" title="Stop playback for this listener"
                                    onclick="stopPlayback('${esc(l.uuid || '')}')">Stop</button>
                            <button class="btn btn-outline-danger btn-sm" title="Ban this listener (IP + ID)" ${l.banned ? 'disabled' : ''}
                                    onclick="banUser('${esc(l.ip || '')}', '${esc(l.uuid || '')}')">Ban</button>
                        </div>
                    </div>
                    <div class="track-meta"><span class="private-ip" tabindex="0" aria-label="IP address; hover or focus to reveal">${esc(l.ip)}</span> &middot; ${new Date(l.last_seen * 1000).toLocaleTimeString()}</div>
                    <div class="track-meta">${l.theme === 'light' ? 'Light' : l.theme === 'dark' ? 'Dark' : 'Unknown'} theme &middot; ${l.effects === true ? 'FX on' : l.effects === false ? 'FX off' : 'FX unknown'}</div>
                    <div class="d-flex gap-2 mt-2">
                        <button class="btn btn-outline-secondary btn-sm" onclick="setListenerPreference('${esc(l.uuid)}', 'theme', '${l.theme === 'light' ? 'dark' : 'light'}')">${l.theme === 'light' ? 'Use dark' : 'Use light'}</button>
                        <button class="btn btn-outline-secondary btn-sm" onclick="setListenerPreference('${esc(l.uuid)}', 'effects', ${l.effects !== true})">${l.effects === true ? 'Turn FX off' : 'Turn FX on'}</button>
                    </div>
                    <div class="track-meta text-truncate" style="max-width:100%">${esc(l.user_agent)}</div>
                    ${volHtml}
                </div>
            </div>`;
        }).join('');
    }

    async function loadData() {
        const [reqRes, lisRes, settingsRes] = await Promise.all([
            fetch('/admin/requests?key=' + authKey),
            fetch('/admin/listeners?key=' + authKey),
            fetch('/admin/settings?key=' + authKey),
        ]);
        if (!reqRes.ok) {
            document.querySelector('.container-fluid').innerHTML = '<p class="text-danger mt-4">Access denied.</p>';
            return;
        }
        const requests = await reqRes.json();
        const listenersData = await lisRes.json();
        const settings = await settingsRes.json();

        requestsById = {};
        requests.forEach(r => { requestsById[r.id] = r; });

        document.getElementById('autoApproveToggle').checked = settings.auto_approve === 'true';

        const pending = requests.filter(r => r.status === 'pending');
        const approved = requests.filter(r => r.status === 'approved');

        document.getElementById('pending-count').textContent = pending.length;
        document.getElementById('approved-count').textContent = approved.length;
        // Match the public count (people only), noting kiosks separately.
        const displays = listenersData.filter(l => l.display).length;
        document.getElementById('listener-count').textContent =
            (listenersData.length - displays) + (displays ? ` +${displays} display` : '');

        document.getElementById('pending-list').innerHTML = renderRequests(pending, true);
        document.getElementById('approved-list').innerHTML = renderRequests(approved, false);
        document.getElementById('listener-list').innerHTML = renderListeners(listenersData);
    }

    async function approveRequest(id) {
        const res = await fetch('/admin/requests/' + id + '/approve?key=' + authKey, {
            method: 'POST', headers: {'Content-Type': 'application/json'}
        });
        const data = await res.json();
        if (!data.success) alert(data.detail || 'Failed to approve');
        loadData();
    }

    async function deleteRequest(id) {
        await fetch('/admin/requests/' + id + '?key=' + authKey, {method: 'DELETE'});
        loadData();
    }

    document.getElementById('autoApproveToggle').addEventListener('change', async function() {
        await fetch('/admin/auto-approve?key=' + authKey, {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({enabled: this.checked})
        });
    });

    async function banSong(requestId) {
        const r = requestsById[requestId];
        if (!r) return;
        if (!confirm('Ban the song "' + (r.track_name || r.spotify_uri) + '"?\\n\\nNo one will be able to request it, and pending requests for it are removed.')) return;
        const res = await fetch('/admin/song-bans?key=' + authKey, {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({songid: r.spotify_uri, track_name: r.track_name, artist_name: r.artist_name})
        });
        if (!res.ok) {
            const d = await res.json().catch(() => ({}));
            alert(d.detail || 'Failed to ban song');
        }
        loadData();
    }

    async function banUser(ip, uuid) {
        if (!ip && !uuid) return;
        const who = [ip, uuid && 'ID ' + uuid.slice(0, 8) + '…'].filter(Boolean).join(' + ');
        const hours = prompt('Ban ' + who + '?\\n\\nDuration in hours (leave blank for permanent):', '');
        if (hours === null) return; // cancelled
        const h = parseFloat(hours);
        const res = await fetch('/admin/bans?key=' + authKey, {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({
                ip, uuid,
                duration_minutes: (hours.trim() && h > 0) ? Math.round(h * 60) : null,
            })
        });
        if (!res.ok) {
            const d = await res.json().catch(() => ({}));
            alert(d.detail || 'Failed to add ban');
        }
        loadData();
    }

    async function setListenerPreference(uuid, field, value) {
        const res = await fetch('/admin/listener/preferences?key=' + authKey, {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({uuid, [field]: value})
        });
        if (!res.ok) {
            const data = await res.json().catch(() => ({}));
            alert(data.detail || 'Could not update listener preferences');
        }
        loadData();
    }

    async function stopPlayback(uuid) {
        const who = uuid ? 'this listener' : 'ALL listeners';
        if (!confirm('Stop playback for ' + who + '?')) return;
        const res = await fetch('/admin/stop?key=' + authKey, {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({uuid: uuid || null})
        });
        if (!res.ok) {
            const d = await res.json().catch(() => ({}));
            alert(d.detail || 'Failed to send stop');
        }
    }

    async function sendMessage(uuid) {
        const who = uuid ? 'this listener' : 'ALL listeners';
        const text = prompt('Popup message to ' + who + ':', '');
        if (text === null || !text.trim()) return;
        const res = await fetch('/admin/message?key=' + authKey, {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({text: text.trim(), uuid: uuid || null})
        });
        if (!res.ok) {
            const d = await res.json().catch(() => ({}));
            alert(d.detail || 'Failed to send message');
        }
    }

    // The admin_key cookie keeps navigation authenticated; only carry the key
    // in the URL when this page itself was opened with one.
    document.getElementById('bansLink').href = '/admin/bans/view' + (authKey ? '?key=' + encodeURIComponent(authKey) : '');

    loadData();
    setInterval(loadData, 10000);
</script>
</body>
</html>"""


BANS_HTML = """<!DOCTYPE html>
<html lang="en" data-bs-theme="dark">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Bans &mdash; Music Sync Admin</title>
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.2/dist/css/bootstrap.min.css" rel="stylesheet" crossorigin="anonymous">
    <style>
        body { font-size: 0.9rem; }
        .mono { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 0.8rem; word-break: break-all; }
        .private-ip { display: inline-block; filter: blur(5px); }
        .private-ip:hover, .private-ip:focus { filter: none; }
    </style>
</head>
<body>
<div class="container py-3" style="max-width:960px">
    <div class="d-flex align-items-center justify-content-between mb-3">
        <h4 class="mb-0">Bans</h4>
        <a id="backLink" href="#" class="btn btn-outline-secondary btn-sm">&larr; Admin</a>
    </div>

    <div class="card mb-3">
        <div class="card-body">
            <div class="row g-2 align-items-end">
                <div class="col-md-3">
                    <label class="form-label mb-1">IP or CIDR</label>
                    <input id="banIp" class="form-control form-control-sm" placeholder="1.2.3.4 or 2001:db8::/56">
                </div>
                <div class="col-md-3">
                    <label class="form-label mb-1">Listener ID (UUID)</label>
                    <input id="banUuid" class="form-control form-control-sm" placeholder="xxxxxxxx-xxxx-...">
                </div>
                <div class="col-md-2">
                    <label class="form-label mb-1">Duration</label>
                    <select id="banDuration" class="form-select form-select-sm">
                        <option value="">Permanent</option>
                        <option value="60">1 hour</option>
                        <option value="360">6 hours</option>
                        <option value="1440">24 hours</option>
                        <option value="10080">7 days</option>
                        <option value="43200">30 days</option>
                    </select>
                </div>
                <div class="col-md-2">
                    <label class="form-label mb-1">Reason</label>
                    <input id="banReason" class="form-control form-control-sm" placeholder="optional">
                </div>
                <div class="col-md-2">
                    <button class="btn btn-danger btn-sm w-100" onclick="addBan()">Add ban</button>
                </div>
            </div>
        </div>
    </div>

    <div id="ban-list"></div>

    <h5 class="mt-4 mb-2">Banned songs</h5>
    <div id="song-ban-list"></div>
</div>

<script>
    const authKey = new URLSearchParams(window.location.search).get('key') || '';
    // The admin_key cookie keeps navigation authenticated; only carry the key
    // in the URL when this page itself was opened with one.
    document.getElementById('backLink').href = '/admin' + (authKey ? '?key=' + encodeURIComponent(authKey) : '');

    function esc(s) {
        return String(s || '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
    }

    async function loadBans() {
        const res = await fetch('/admin/bans?key=' + authKey);
        if (!res.ok) {
            document.getElementById('ban-list').innerHTML = '<p class="text-danger mt-4">Access denied.</p>';
            return;
        }
        const bans = await res.json();
        if (bans.length === 0) {
            document.getElementById('ban-list').innerHTML = '<p class="text-secondary">No active bans.</p>';
            return;
        }
        document.getElementById('ban-list').innerHTML = `
            <div class="table-responsive"><table class="table table-sm align-middle">
                <thead><tr><th>IP</th><th>Listener ID</th><th>Reason</th><th>Expires</th><th>Banned at</th><th></th></tr></thead>
                <tbody>${bans.map(b => `
                    <tr>
                        <td class="mono"><span class="private-ip" tabindex="0" aria-label="IP address; hover or focus to reveal">${esc(b.ip) || '&mdash;'}</span></td>
                        <td class="mono">${esc(b.uuid) || '<span class="text-secondary">&mdash;</span>'}</td>
                        <td>${esc(b.reason)}</td>
                        <td>${b.expires_at ? esc(b.expires_at) + ' UTC' : '<span class="badge bg-danger">permanent</span>'}</td>
                        <td>${esc(b.created_at)} UTC</td>
                        <td class="text-end"><button class="btn btn-outline-success btn-sm" onclick="unban(${b.id})">Unban</button></td>
                    </tr>`).join('')}
                </tbody>
            </table></div>`;
    }

    async function addBan() {
        const ip = document.getElementById('banIp').value.trim();
        const uuid = document.getElementById('banUuid').value.trim();
        const duration = document.getElementById('banDuration').value;
        const reason = document.getElementById('banReason').value.trim();
        if (!ip && !uuid) { alert('Enter an IP and/or a listener ID.'); return; }
        const res = await fetch('/admin/bans?key=' + authKey, {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({ip, uuid, reason, duration_minutes: duration ? parseInt(duration, 10) : null})
        });
        if (!res.ok) {
            const d = await res.json().catch(() => ({}));
            alert(d.detail || 'Failed to add ban');
            return;
        }
        document.getElementById('banIp').value = '';
        document.getElementById('banUuid').value = '';
        document.getElementById('banReason').value = '';
        loadBans();
    }

    async function unban(id) {
        await fetch('/admin/bans/' + id + '?key=' + authKey, {method: 'DELETE'});
        loadBans();
    }

    async function loadSongBans() {
        const res = await fetch('/admin/song-bans?key=' + authKey);
        if (!res.ok) return;
        const bans = await res.json();
        if (bans.length === 0) {
            document.getElementById('song-ban-list').innerHTML = '<p class="text-secondary">No banned songs.</p>';
            return;
        }
        document.getElementById('song-ban-list').innerHTML = `
            <div class="table-responsive"><table class="table table-sm align-middle">
                <thead><tr><th>Track</th><th>Artist</th><th>Song ID</th><th>Banned at</th><th></th></tr></thead>
                <tbody>${bans.map(b => `
                    <tr>
                        <td>${esc(b.track_name) || '<span class="text-secondary">&mdash;</span>'}</td>
                        <td>${esc(b.artist_name)}</td>
                        <td class="mono">${esc(b.songid)}</td>
                        <td>${esc(b.created_at)} UTC</td>
                        <td class="text-end"><button class="btn btn-outline-success btn-sm" onclick="unbanSong(${b.id})">Unban</button></td>
                    </tr>`).join('')}
                </tbody>
            </table></div>`;
    }

    async function unbanSong(id) {
        await fetch('/admin/song-bans/' + id + '?key=' + authKey, {method: 'DELETE'});
        loadSongBans();
    }

    loadBans();
    loadSongBans();
</script>
</body>
</html>"""


# The audio monitor drawn in the admin panel. Its script and styles live here; its data
# comes from the audio container's control API, which is reachable only over the Compose
# network. Only admins reach either, and only these control paths, by method.
AUDIO_MONITOR_FILES = {
    "monitor.js": ("audio-monitor.js", "text/javascript; charset=utf-8"),
    "monitor.css": ("audio-monitor.css", "text/css; charset=utf-8"),
}
STATIC_DIR = Path(__file__).resolve().parent / "static"
AUDIO_CONTROL_ROUTES = {
    "GET": {"api/state", "api/graph", "api/meters"},
    "POST": {"api/bypass", "api/agc", "api/graph", "api/graph/reload"},
}


@app.get("/admin/audio/{name}")
async def admin_audio_asset(name: str, _: None = Depends(require_admin)):
    if name not in AUDIO_MONITOR_FILES:
        raise HTTPException(status_code=404, detail="Not found")
    filename, media_type = AUDIO_MONITOR_FILES[name]
    return FileResponse(STATIC_DIR / filename, media_type=media_type, headers={"Cache-Control": "no-cache"})


@app.api_route("/admin/audio/{path:path}", methods=["GET", "POST"])
async def admin_audio_proxy(path: str, request: Request, _: None = Depends(require_admin)):
    if path not in AUDIO_CONTROL_ROUTES.get(request.method, set()):
        raise HTTPException(status_code=404, detail="Not found")
    url = f"{AUDIO_CONTROL_URL}/{path}"
    if path == "api/meters":
        # Server-sent events: relay the stream as it arrives.
        client = httpx.AsyncClient(timeout=httpx.Timeout(10, read=None))
        try:
            upstream = await client.send(client.build_request("GET", url), stream=True)
        except httpx.HTTPError:
            await client.aclose()
            return JSONResponse({"error": "audio control is unreachable"}, status_code=502)

        async def relay():
            try:
                async for chunk in upstream.aiter_raw():
                    yield chunk
            finally:
                await upstream.aclose()
                await client.aclose()

        return StreamingResponse(
            relay(),
            status_code=upstream.status_code,
            media_type="text/event-stream",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )
    headers = {}
    body = None
    if request.method == "POST":
        body = await request.body()
        headers["Content-Type"] = request.headers.get("content-type", "application/json")
    try:
        # Applying a graph waits for the new processor to warm up and take over.
        async with httpx.AsyncClient(timeout=30) as client:
            upstream = await client.request(request.method, url, content=body, headers=headers)
    except httpx.HTTPError:
        return JSONResponse({"error": "audio control is unreachable"}, status_code=502)
    return Response(
        upstream.content,
        status_code=upstream.status_code,
        media_type=upstream.headers.get("content-type"),
        headers={"Cache-Control": "no-store"},
    )


@app.get("/admin", response_class=HTMLResponse)
async def admin_panel(request: Request):
    return admin_page_response(request, ADMIN_HTML)


@app.get("/admin/requests")
async def admin_get_requests(_: None = Depends(require_admin)):
    db = get_db()
    try:
        rows = db.execute(
            "SELECT * FROM requests WHERE status IN ('pending', 'approved') ORDER BY created_at DESC LIMIT 100"
        ).fetchall()
    finally:
        db.close()
    result = []
    for r in rows:
        d = dict(r)
        d["requester_banned"] = is_banned(d.get("requester_ip"))
        d["song_banned"] = is_song_banned(d.get("spotify_uri"))
        result.append(d)
    return result


@app.post("/admin/requests/{request_id}/approve")
async def admin_approve_request(request_id: int, _: None = Depends(require_admin)):
    db = get_db()
    try:
        row = db.execute("SELECT * FROM requests WHERE id=?", (request_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Request not found")
        await librespot.add_to_queue(row["spotify_uri"])
        await refresh_queue_now(librespot)
        db.execute("UPDATE requests SET status='approved' WHERE id=?", (request_id,))
        db.commit()
        return {"success": True}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        db.close()


@app.delete("/admin/requests/{request_id}")
async def admin_delete_request(request_id: int, _: None = Depends(require_admin)):
    db = get_db()
    try:
        db.execute("UPDATE requests SET status='deleted' WHERE id=?", (request_id,))
        db.commit()
        return {"success": True}
    finally:
        db.close()


@app.get("/admin/listeners")
async def admin_get_listeners(_: None = Depends(require_admin)):
    # Collect all UUIDs that have voted to skip the current song
    current_voters: set[str] = set()
    for voters in vote_skips.values():
        for entry in voters:
            uuid = entry.split(":")[0]
            current_voters.add(uuid)

    result = []
    for uuid, data in listeners.items():
        result.append({
            **data,
            "voted_skip": uuid in current_voters,
            "banned": is_banned(data.get("ip"), uuid),
        })
    return result


@app.get("/admin/settings")
async def admin_get_settings(_: None = Depends(require_admin)):
    db = get_db()
    try:
        row = db.execute("SELECT value FROM settings WHERE key='auto_approve'").fetchone()
        return {"auto_approve": row["value"] if row else "false"}
    finally:
        db.close()


class AutoApprovePayload(BaseModel):
    enabled: bool


@app.post("/admin/auto-approve")
async def admin_set_auto_approve(payload: AutoApprovePayload, _: None = Depends(require_admin)):
    db = get_db()
    try:
        value = "true" if payload.enabled else "false"
        db.execute("INSERT OR REPLACE INTO settings VALUES ('auto_approve', ?)", (value,))
        db.commit()
        return {"success": True, "auto_approve": value}
    finally:
        db.close()

# ─── Remote control ──────────────────────────────────────────────────────────
# Push commands to connected clients over SSE. Targeting is best-effort: SSE is
# broadcast-to-all, so a uuid in the payload just tells non-matching clients to
# ignore it. uuid of None/"" means everyone.

class ListenerPreferencesPayload(BaseModel):
    uuid: str
    theme: Optional[Literal["light", "dark"]] = None
    effects: Optional[bool] = None


@app.post("/admin/listener/preferences")
async def admin_set_listener_preferences(payload: ListenerPreferencesPayload, _: None = Depends(require_admin)):
    if not UUID_RE.match(payload.uuid):
        raise HTTPException(status_code=400, detail="Invalid UUID")
    if payload.uuid not in listeners:
        raise HTTPException(status_code=404, detail="Listener is no longer connected")
    changes = {}
    if payload.theme is not None:
        changes["theme"] = payload.theme
    if payload.effects is not None:
        changes["effects"] = payload.effects
    if not changes:
        raise HTTPException(status_code=400, detail="Provide a theme or effects setting")
    listeners[payload.uuid].update(changes)
    await broadcast_both("preferences", {"uuid": payload.uuid, **changes})
    return {"success": True}


class StopPayload(BaseModel):
    uuid: Optional[str] = None


@app.post("/admin/stop")
async def admin_stop_playback(payload: StopPayload, _: None = Depends(require_admin)):
    await broadcast_both("stop", {"uuid": (payload.uuid or "").strip() or None})
    return {"success": True}


class AnnouncePayload(BaseModel):
    text: str
    uuid: Optional[str] = None


@app.post("/admin/message")
async def admin_send_message(payload: AnnouncePayload, _: None = Depends(require_admin)):
    text = payload.text.strip()
    if not text:
        raise HTTPException(status_code=400, detail="No message text provided")
    await broadcast_both(
        "announce",
        {"text": text[:500], "uuid": (payload.uuid or "").strip() or None},
    )
    return {"success": True}

# ─── Bans ────────────────────────────────────────────────────────────────────

class BanPayload(BaseModel):
    ip: Optional[str] = None
    uuid: Optional[str] = None
    reason: Optional[str] = None
    duration_minutes: Optional[int] = None  # None/0 = permanent


@app.get("/admin/bans")
async def admin_get_bans(_: None = Depends(require_admin)):
    db = get_db()
    try:
        # Expired temporary bans are dead weight — sweep them while we're here.
        db.execute("DELETE FROM bans WHERE expires_at IS NOT NULL AND expires_at <= datetime('now')")
        db.commit()
        rows = db.execute("SELECT * FROM bans ORDER BY created_at DESC").fetchall()
        return [dict(r) for r in rows]
    finally:
        db.close()


@app.post("/admin/bans")
async def admin_add_ban(payload: BanPayload, _: None = Depends(require_admin)):
    # IPv6 bans are widened to the household's /64 (see normalize_ban_ip)
    ip = normalize_ban_ip(payload.ip)
    uuid = (payload.uuid or "").strip()
    if not ip and not uuid:
        raise HTTPException(status_code=400, detail="Provide an IP and/or a listener ID to ban")
    minutes = payload.duration_minutes or 0
    db = get_db()
    try:
        if minutes > 0:
            db.execute(
                """INSERT INTO bans (ip, uuid, reason, expires_at)
                   VALUES (?, ?, ?, datetime('now', '+' || ? || ' minutes'))""",
                (ip, uuid, (payload.reason or "").strip(), minutes),
            )
        else:
            db.execute(
                "INSERT INTO bans (ip, uuid, reason) VALUES (?, ?, ?)",
                (ip, uuid, (payload.reason or "").strip()),
            )
        db.commit()
        return {"success": True}
    finally:
        db.close()


@app.delete("/admin/bans/{ban_id}")
async def admin_delete_ban(ban_id: int, _: None = Depends(require_admin)):
    db = get_db()
    try:
        db.execute("DELETE FROM bans WHERE id=?", (ban_id,))
        db.commit()
        return {"success": True}
    finally:
        db.close()


class SongBanPayload(BaseModel):
    songid: str
    track_name: Optional[str] = None
    artist_name: Optional[str] = None


@app.get("/admin/song-bans")
async def admin_get_song_bans(_: None = Depends(require_admin)):
    db = get_db()
    try:
        rows = db.execute("SELECT * FROM song_bans ORDER BY created_at DESC").fetchall()
        return [dict(r) for r in rows]
    finally:
        db.close()


@app.post("/admin/song-bans")
async def admin_add_song_ban(payload: SongBanPayload, _: None = Depends(require_admin)):
    sid = normalize_songid(payload.songid)
    if not sid:
        raise HTTPException(status_code=400, detail="No song id provided")
    db = get_db()
    try:
        db.execute(
            "INSERT OR IGNORE INTO song_bans (songid, track_name, artist_name) VALUES (?, ?, ?)",
            (sid, (payload.track_name or "").strip(), (payload.artist_name or "").strip()),
        )
        # A banned song has no business sitting in the pending queue either
        db.execute(
            "UPDATE requests SET status='deleted' WHERE status='pending' AND spotify_uri IN (?, ?)",
            (sid, f"spotify:track:{sid}"),
        )
        db.commit()
        return {"success": True}
    finally:
        db.close()


@app.delete("/admin/song-bans/{ban_id}")
async def admin_delete_song_ban(ban_id: int, _: None = Depends(require_admin)):
    db = get_db()
    try:
        db.execute("DELETE FROM song_bans WHERE id=?", (ban_id,))
        db.commit()
        return {"success": True}
    finally:
        db.close()


@app.get("/admin/bans/view", response_class=HTMLResponse)
async def bans_page(request: Request):
    return admin_page_response(request, BANS_HTML)
