import asyncio
import json
import logging
import re
import time
from collections import deque
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal, Optional
from urllib.parse import urlsplit

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
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
from automate import AutomateController
from librespot import LibrespotClient
from spotify import COVER_ID_RE, SpotifyClient
from sse import (
    broadcast_both,
    broadcaster,
    cleanup_listeners,
    current_track_snapshot,
    cleanup_old_requests,
    REALTIME_SNAPSHOT_MAX_AGE,
    latest_snapshot,
    live_input_state,
    spotify_playing,
    live_metadata_for_clients,
    committed_metadata_for_clients,
    poll_live_input,
    poll_spotify,
    realtime_broadcaster,
    SSE_CLOSED,
    refresh_queue_now,
    refresh_token_loop,
)
from storage import (
    active_bans,
    active_listener_count,
    add_ban,
    add_song_ban,
    ban_matches,
    check_rate_limit,
    check_submission_id,
    delete_ban,
    delete_pending_request,
    delete_song_ban,
    get_history,
    get_request,
    init_db,
    insert_request,
    is_banned,
    is_song_banned,
    list_admin_requests,
    list_deleted_requests,
    list_bans,
    list_song_bans,
    normalize_ban_ip,
    normalize_songid,
    listeners,
    read_auto_approve,
    read_automate,
    move_pending_request,
    restore_deleted_request,
    set_request_status,
    upcoming_queue,
    vote_skips,
    write_auto_approve,
)

logging.basicConfig(level=logging.INFO)
# httpx logs every request at INFO; the playback poll makes one each second.
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

spotify_client = SpotifyClient()
librespot = LibrespotClient(spotify_client)
automate_controller = AutomateController(librespot)
request_moderation_lock = asyncio.Lock()

# Shared outbound HTTP clients, one per upstream. Created in the lifespan and closed on
# shutdown; http_client() also creates one on first use so code paths that run without
# the lifespan (tests, tools) still work. Timeouts are per client, and the meter stream
# overrides its read timeout per request.
HTTP_CLIENT_TIMEOUTS = {"turnstile": 10, "cover": 10, "audio": 30}
_http_clients: dict[str, httpx.AsyncClient] = {}


def http_client(name: str) -> httpx.AsyncClient:
    client = _http_clients.get(name)
    if client is None:
        client = httpx.AsyncClient(timeout=HTTP_CLIENT_TIMEOUTS[name])
        _http_clients[name] = client
    return client


async def close_http_clients() -> None:
    clients = list(_http_clients.values())
    _http_clients.clear()
    for client in clients:
        await client.aclose()


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    for name in HTTP_CLIENT_TIMEOUTS:
        http_client(name)
    tasks = [
        asyncio.create_task(poll_spotify(librespot, listeners, vote_skips)),
        asyncio.create_task(poll_live_input()),
        asyncio.create_task(cleanup_listeners(listeners)),
        asyncio.create_task(refresh_token_loop(spotify_client)),
        asyncio.create_task(cleanup_old_requests()),
        asyncio.create_task(automate_controller.run()),
    ]
    try:
        yield
    finally:
        # Cancel background loops so SIGTERM/docker stop does not hang on them.
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await close_http_clients()
        await librespot.aclose()


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
    resp = await http_client("turnstile").post(
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
                    item = await asyncio.wait_for(queue.get(), timeout=20)
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
                    continue
                if item is SSE_CLOSED:
                    return  # dropped for falling behind; the client reconnects
                event, data = item
                yield f"event: {event}\ndata: {json.dumps(data)}\n\n"
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
            if "music" in allowed:
                current, polled_at = await current_track_snapshot(librespot)
                yield f"event: metadata\ndata: {json.dumps(live_metadata_for_clients(current, polled_at))}\n\n"
                yield f"event: history\ndata: {json.dumps({'history': get_history()})}\n\n"
                yield f"event: queue\ndata: {json.dumps({'queue': upcoming_queue})}\n\n"
                yield f"event: source\ndata: {json.dumps({'live': live_input_state['realtime']})}\n\n"
            if "listeners" in allowed:
                yield f"event: listeners\ndata: {json.dumps({'count': active_listener_count()})}\n\n"

            while True:
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=20)
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
                    continue
                if item is SSE_CLOSED:
                    return  # dropped for falling behind; the client reconnects
                event, data = item
                if _REALTIME_EVENT_CATEGORY.get(event) not in allowed:
                    continue
                yield f"event: {event}\ndata: {json.dumps(data)}\n\n"
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
    if await asyncio.to_thread(is_banned, request.client.host, payload.uuid):
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

    if await asyncio.to_thread(is_banned, ip, payload.uuid):
        raise HTTPException(status_code=403, detail=BAN_MESSAGE)
    if await asyncio.to_thread(is_song_banned, payload.uri):
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

    auto_approve = (await asyncio.to_thread(read_auto_approve)) == "true"

    status = "pending"
    message = "Thanks, your request has been added to the queue for approval!"
    if auto_approve:
        await librespot.add_to_queue(payload.uri)
        await refresh_queue_now(librespot)
        status = "approved"
        message = "Your request has been automatically approved and added to the queue!"

    await asyncio.to_thread(
        insert_request,
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
    return {"success": True, "message": message}

# ─── Vote Skip ───────────────────────────────────────────────────────────────

class SkipPayload(BaseModel):
    uuid: str
    songid: str
    turnstile: str


@app.post("/skip")
async def vote_skip(payload: SkipPayload, request: Request):
    ip = request.client.host

    if await asyncio.to_thread(is_banned, ip, payload.uuid):
        raise HTTPException(status_code=403, detail="You have been banned from voting to skip.")
    if not await verify_turnstile(payload.turnstile, ip):
        raise HTTPException(status_code=400, detail="Turnstile verification failed")

    if payload.uuid not in listeners:
        raise HTTPException(status_code=400, detail="Invalid or inactive listener")

    if not check_rate_limit("vote", ip, 5, 60):
        raise HTTPException(status_code=429, detail="Too many vote attempts. Please wait before trying again.")

    current, _ = await current_track_snapshot(librespot)
    current_song_id = current.get("songid", "")
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
        except Exception:
            raise HTTPException(status_code=500, detail="Failed to skip the song")

    await broadcast_both("skipvotes", {"song": song_id, "count": count, "needed": needed})
    return {"success": True, "message": "Vote recorded", "count": count, "needed": needed}


@app.get("/skip/stats")
async def skip_stats():
    current, _ = await current_track_snapshot(librespot)
    song_id = current.get("songid", "")
    count = len(vote_skips.get(song_id, set()))
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
        resp = await http_client("cover").get(f"https://i.scdn.co/image/{image_id}")
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
        /* Deck: transport, toggles, progress and the input graph in one card. */
        .jmm-deck .deck-controls { display: grid; grid-template-columns: repeat(3, 1fr); gap: 0.5rem; }
        .jmm-deck .deck-controls .btn { min-height: 3rem; font-size: 1rem; font-weight: 600; }
        .jmm-deck .btn-toggle { background: transparent; color: var(--bs-body-color); border: 2px solid var(--bs-border-color); }
        .jmm-deck .btn-toggle[aria-pressed="true"] { color: #fff; border-color: transparent; }
        #automateToggle[aria-pressed="true"] { background: #198754; }
        #st-listen[aria-pressed="true"] { background: #0d6efd; }
        #st-mic-toggle[aria-pressed="true"] { background: #dc3545; animation: deck-live 1.2s ease-in-out infinite; }
        @keyframes deck-live { 50% { box-shadow: 0 0 0 4px rgba(220, 53, 69, 0.35); } }
        @media (min-width: 768px) { .jmm-deck .deck-controls { grid-template-columns: repeat(6, 1fr); } }
        .jmm-deck .deck-aux { display: flex; flex-wrap: wrap; align-items: center; gap: 0.4rem 0.9rem; margin-top: 0.6rem; font-size: 0.8rem; }
        .jmm-deck .deck-aux label { white-space: nowrap; margin: 0; }
        .jmm-deck .deck-vol { width: 8rem; margin: 0; }
        .jmm-deck .deck-mic { width: auto; max-width: 16rem; }
        .jmm-deck .deck-meter { width: 7rem; }
        .jmm-deck .deck-now { display: flex; gap: 0.75rem; align-items: center; margin-top: 0.75rem; }
        .jmm-deck .deck-upnext { display: flex; gap: 0.5rem; align-items: center; min-width: 0; margin-top: 0.6rem; font-size: 0.8rem; color: var(--bs-secondary-color); }
        .jmm-deck .deck-upnext[hidden] { display: none; }
        .jmm-deck .deck-upnext-cover { width: 32px; height: 32px; border-radius: 4px; object-fit: cover; flex-shrink: 0; }
        .jmm-deck .deck-upnext-blank { width: 32px; height: 32px; border-radius: 4px; flex-shrink: 0; }
        .jmm-deck .deck-upnext-text { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
        .jmm-deck .deck-bar { height: 6px; border-radius: 3px; background: var(--bs-border-color); overflow: hidden; }
        .jmm-deck .deck-bar-fill { height: 100%; width: 0; background: #0d6efd; }
        .jmm-deck .deck-time { font-variant-numeric: tabular-nums; }
        .jmm-deck .deck-input { margin-top: 0.75rem; }
        #request-action-status:empty { display: none; }
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

    <!-- Deck: transport, toggles, the Spotify progress and the input graph. The listen and mic
         controls are bound by studio.js, which talks to the audio container through the admin proxy.
         The input graph is the audio monitor's input view; studio.js shows it only while Listen is on. -->

    <div id="deck" class="card jmm-deck jmm-studio" data-base="/admin/audio/">
        <div class="card-body">
            <div class="deck-controls">
                <button id="deckPrev" type="button" class="btn btn-outline-secondary" title="Previous track">&#9664;&#9664; Prev</button>
                <button id="deckPlay" type="button" class="btn btn-outline-primary" title="Play or pause">Play / Pause</button>
                <button id="deckNext" type="button" class="btn btn-outline-secondary" title="Next track">Next &#9654;&#9654;</button>
                <button id="automateToggle" type="button" class="btn btn-toggle" aria-pressed="false"
                        title="Carry playback on into the next song">Automate: Off</button>
                <button id="st-listen" type="button" class="btn btn-toggle" aria-pressed="false"
                        title="Listen to the Spotify input, before processing">Listen: Off</button>
                <button id="st-mic-toggle" type="button" class="btn btn-toggle" aria-pressed="false"
                        title="Send the microphone into the audio input">Mic: Off</button>
            </div>
            <div class="deck-aux">
                <label for="st-vol">Listen <span id="st-volVal" class="font-monospace">5%</span></label>
                <input id="st-vol" type="range" class="form-range deck-vol" min="0" max="100" value="5">
                <select id="st-mic" class="form-select form-select-sm deck-mic" aria-label="Microphone device"
                        title="Use headphones: the speakers would feed back into the mic"></select>
                <div class="meter deck-meter" aria-hidden="true"><div id="st-meter"></div></div>
                <span id="st-listenStatus" class="text-secondary" role="status">Off</span>
                <span id="st-micStatus" class="text-secondary" role="status">Off</span>
            </div>
            <div class="deck-now">
                <img id="deckCover" class="album-art" alt="" hidden>
                <div id="deckCoverBlank" class="album-art-placeholder bg-secondary-subtle"></div>
                <div class="flex-grow-1 overflow-hidden">
                    <div class="d-flex justify-content-between align-items-baseline gap-2">
                        <div id="deckTitle" class="track-title text-truncate">Nothing playing</div>
                        <span id="deckState" class="badge bg-secondary">Unknown</span>
                    </div>
                    <div id="deckArtist" class="track-meta text-truncate"></div>
                    <div class="deck-bar mt-1"><div id="deckFill" class="deck-bar-fill"></div></div>
                    <div class="d-flex justify-content-between track-meta deck-time mt-1">
                        <span id="deckElapsed">0:00</span><span id="deckDuration">0:00</span>
                    </div>
                </div>
            </div>
            <div id="deckUpNext" class="deck-upnext" hidden>
                <img id="deckUpNextCover" class="deck-upnext-cover" alt="" hidden>
                <div id="deckUpNextBlank" class="deck-upnext-blank bg-secondary-subtle"></div>
                <span class="flex-shrink-0">Up next:</span>
                <span id="deckUpNextText" class="deck-upnext-text"></span>
            </div>
            <div id="deckInput" class="deck-input" data-jmm-monitor data-embedded data-input-only data-base="/admin/audio/" hidden></div>
        </div>
    </div>
    <script src="/admin/audio/studio.js"></script>

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
            <p class="small text-secondary mb-2">Move requests to set approval order. Each approval sends that song to the player queue.</p>
            <div id="request-action-status" class="small mb-2" role="status" aria-live="polite"></div>
            <div id="pending-list"></div>

            <details class="mt-3">
                <summary class="section-header">Deleted Requests <span id="deleted-count" class="badge bg-secondary ms-1">0</span></summary>
                <div id="deleted-list" class="mt-2"></div>
            </details>

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
    let requestMutationInFlight = false;

    // Audio monitor (meters, bypasses, processing graph) from the audio container, through
    // the admin proxy. Nothing is fetched until it is first shown, and hiding it stops the
    // meter stream and polling. Its calls carry the admin key like the rest of this page.
    let monitorScript = null;
    // Loads the monitor's styles and script once, then mounts the monitor into box and resolves
    // with its { start, stop } handle. The studio deck's input graph uses the same loader.
    function loadAudioMonitor(box) {
        if (!monitorScript) {
            monitorScript = new Promise((resolve, reject) => {
                const q = authKey ? '?key=' + encodeURIComponent(authKey) : '';
                const css = document.createElement('link');
                css.rel = 'stylesheet';
                css.href = '/admin/audio/monitor.css' + q;
                document.head.append(css);
                const js = document.createElement('script');
                js.src = '/admin/audio/monitor.js' + q;
                js.onload = resolve;
                js.onerror = () => { monitorScript = null; reject(new Error('the audio monitor could not load')); };
                document.head.append(js);
            });
        }
        return monitorScript.then(() => {
            box.dataset.key = authKey;
            return window.JmmMonitor.mount(box);
        });
    }
    window.loadAudioMonitor = loadAudioMonitor;

    (() => {
        const box = document.getElementById('audioMonitor');
        const toggle = document.getElementById('audioToggle');
        toggle.addEventListener('click', () => {
            const show = box.hidden;
            box.hidden = !show;
            toggle.textContent = show ? 'Hide' : 'Show';
            toggle.setAttribute('aria-expanded', String(show));
            if (!show) {
                if (box.jmmMonitor) box.jmmMonitor.stop();
                return;
            }
            // Hidden again before the monitor loaded: the check keeps it from starting.
            loadAudioMonitor(box).then((monitor) => { if (!box.hidden) monitor.start(); })
                .catch((err) => console.error(err));
        });
    })();

    function esc(s) {
        return String(s || '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
    }

    function renderRequests(requests, pending) {
        if (requests.length === 0) return '<p class="text-secondary">None.</p>';
        return requests.map((r, index) => `
            <div class="card ${pending ? '' : 'border-success'}">
                <div class="card-body d-flex gap-2 align-items-center">
                    ${r.cover_url
                        ? `<img src="${esc(r.cover_url)}" class="album-art" alt="">`
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
                        ${index > 0 ? `<button class="btn btn-outline-secondary btn-sm" aria-label="Move ${esc(r.track_name || 'request')} up" title="Move up" ${requestMutationInFlight ? 'disabled' : ''} onclick="moveRequest(${r.id}, 'up')">&#8593;</button>` : ''}
                        ${index < requests.length - 1 ? `<button class="btn btn-outline-secondary btn-sm" aria-label="Move ${esc(r.track_name || 'request')} down" title="Move down" ${requestMutationInFlight ? 'disabled' : ''} onclick="moveRequest(${r.id}, 'down')">&#8595;</button>` : ''}
                        <button class="btn btn-success btn-sm" aria-label="Approve ${esc(r.track_name || 'request')}" ${requestMutationInFlight ? 'disabled' : ''} onclick="approveRequest(${r.id})">Approve</button>
                        <button class="btn btn-danger btn-sm" aria-label="Delete ${esc(r.track_name || 'request')}" ${requestMutationInFlight ? 'disabled' : ''} onclick="deleteRequest(${r.id})">Delete</button>` : ''}
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
        const [reqRes, lisRes, settingsRes, deletedRes] = await Promise.all([
            fetch('/admin/requests?key=' + authKey),
            fetch('/admin/listeners?key=' + authKey),
            fetch('/admin/settings?key=' + authKey),
            fetch('/admin/requests/deleted?key=' + authKey),
        ]);
        if (!reqRes.ok) {
            document.querySelector('.container-fluid').innerHTML = '<p class="text-danger mt-4">Access denied.</p>';
            return;
        }
        const requests = await reqRes.json();
        const listenersData = await lisRes.json();
        const settings = await settingsRes.json();
        const deleted = deletedRes.ok ? await deletedRes.json() : [];

        requestsById = {};
        requests.forEach(r => { requestsById[r.id] = r; });

        document.getElementById('autoApproveToggle').checked = settings.auto_approve === 'true';

        const pending = requests.filter(r => r.status === 'pending');
        const approved = requests.filter(r => r.status === 'approved');

        document.getElementById('pending-count').textContent = pending.length;
        document.getElementById('approved-count').textContent = approved.length;
        document.getElementById('deleted-count').textContent = deleted.length;
        // Match the public count (people only), noting kiosks separately.
        const displays = listenersData.filter(l => l.display).length;
        document.getElementById('listener-count').textContent =
            (listenersData.length - displays) + (displays ? ` +${displays} display` : '');

        document.getElementById('pending-list').innerHTML = renderRequests(pending, true);
        document.getElementById('approved-list').innerHTML = renderRequests(approved, false);
        document.getElementById('deleted-list').innerHTML = deleted.length ? deleted.map(r => `
            <div class="card">
                <div class="card-body d-flex gap-2 align-items-center">
                    ${r.cover_url ? `<img src="${esc(r.cover_url)}" class="album-art" alt="">` : '<div class="album-art-placeholder bg-secondary-subtle"></div>'}
                    <div class="flex-grow-1 overflow-hidden">
                        <div class="track-title text-truncate">${esc(r.track_name || r.spotify_uri)} ${r.song_banned ? '<span class="badge bg-danger">song banned</span>' : ''}</div>
                        <div class="track-meta text-truncate">${esc(r.artist_name || '')} &middot; From ${esc(r.requester_name || 'Anonymous')} &middot; ${esc(r.created_at || '')}</div>
                    </div>
                    <button class="btn btn-outline-primary btn-sm" aria-label="Restore ${esc(r.track_name || 'request')}" ${requestMutationInFlight || r.song_banned ? 'disabled' : ''} onclick="restoreRequest(${r.id})">Restore</button>
                </div>
            </div>`).join('') : '<p class="text-secondary">None.</p>';
        document.getElementById('listener-list').innerHTML = renderListeners(listenersData);
    }

    async function moderateRequest(id, action, options = {}) {
        if (requestMutationInFlight) return;
        requestMutationInFlight = true;
        const status = document.getElementById('request-action-status');
        status.className = 'small text-secondary';
        status.textContent = 'Updating request…';
        document.querySelectorAll('#pending-list button, #deleted-list button').forEach(button => { button.disabled = true; });
        try {
            const res = await fetch('/admin/requests/' + id + action + '?key=' + encodeURIComponent(authKey), {
                method: action === '' ? 'DELETE' : 'POST',
                signal: AbortSignal.timeout(15000),
                ...(options.body ? {headers: {'Content-Type': 'application/json'}, body: JSON.stringify(options.body)} : {})
            });
            const data = await res.json().catch(() => ({}));
            if (!res.ok) throw new Error(data.detail || 'Request update failed');
            status.textContent = options.success || 'Request updated.';
        } catch (error) {
            status.className = 'small text-danger';
            status.textContent = error.message || 'Request update failed';
        } finally {
            requestMutationInFlight = false;
            try {
                await loadData();
            } catch (error) {
                status.className = 'small text-danger';
                status.textContent = 'Request action finished, but the lists could not refresh. ' + (error.message || '');
            }
        }
    }

    function approveRequest(id) { return moderateRequest(id, '/approve', {success: 'Approved and added to the player queue.'}); }
    function deleteRequest(id) { return moderateRequest(id, '', {success: 'Request moved to Deleted Requests.'}); }
    function moveRequest(id, direction) { return moderateRequest(id, '/move', {body: {direction}, success: 'Request order updated.'}); }
    function restoreRequest(id) { return moderateRequest(id, '/restore', {success: 'Request restored to the end of the pending list.'}); }

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
    // Deck: transport, automate and the Spotify progress bar. Track and playing state come from
    // the undelayed poller, polled every second. The progress bar is interpolated between polls
    // from the position and the time it arrived. The input graph under it is studio.js's concern.
    const deckPlay = document.getElementById('deckPlay');
    const automateButton = document.getElementById('automateToggle');
    const deckFill = document.getElementById('deckFill');
    const deckElapsed = document.getElementById('deckElapsed');
    const deckDuration = document.getElementById('deckDuration');
    const deckState = document.getElementById('deckState');
    const deckTitle = document.getElementById('deckTitle');
    const deckArtist = document.getElementById('deckArtist');
    const deckCover = document.getElementById('deckCover');
    const deckCoverBlank = document.getElementById('deckCoverBlank');
    const deckUpNext = document.getElementById('deckUpNext');
    const deckUpNextText = document.getElementById('deckUpNextText');
    const deckUpNextCover = document.getElementById('deckUpNextCover');
    const deckUpNextBlank = document.getElementById('deckUpNextBlank');

    let deckPlaying = null;   // true, false, or null before the first poll
    let deckTrack = null;     // {songid, title, artist, cover, progress_ms, duration_ms}
    let deckAnchor = null;    // {progress_ms, at}: position at the moment the poll was read
    let deckSongId = null;
    let deckNextId = null;
    let deckShownSecond = -1;

    function fmtTime(ms) {
        const s = Math.max(0, Math.floor(ms / 1000));
        return Math.floor(s / 60) + ':' + String(s % 60).padStart(2, '0');
    }

    function renderAutomate(on) {
        automateButton.textContent = 'Automate: ' + (on ? 'On' : 'Off');
        automateButton.setAttribute('aria-pressed', String(on));
    }

    function renderPlayState() {
        deckPlay.innerHTML = deckPlaying === true ? '&#10074;&#10074; Pause'
            : deckPlaying === false ? '&#9654; Play' : 'Play / Pause';
        deckState.textContent = deckPlaying === true ? 'Playing' : deckPlaying === false ? 'Paused' : 'Unknown';
        deckState.className = 'badge ' + (deckPlaying === true ? 'bg-success' : 'bg-secondary');
    }

    function renderTrack(track) {
        const songId = track ? track.songid : null;
        if (songId !== deckSongId) {
            deckSongId = songId;
            deckTitle.textContent = track ? track.title || 'Unknown title' : 'Nothing playing';
            deckArtist.textContent = track ? track.artist || '' : '';
            if (track && track.cover) {
                deckCover.src = track.cover;
                deckCover.hidden = false;
                deckCoverBlank.hidden = true;
            } else {
                deckCover.removeAttribute('src');
                deckCover.hidden = true;
                deckCoverBlank.hidden = false;
            }
        }
        deckDuration.textContent = track ? fmtTime(track.duration_ms) : '0:00';
    }

    // The queued song, one line under the deck. Text is set with textContent, so titles are not parsed as HTML.
    function renderUpNext(next) {
        const nextId = next ? next.songid : null;
        if (nextId !== deckNextId) {
            deckNextId = nextId;
            if (next && next.cover) {
                deckUpNextCover.src = next.cover;
                deckUpNextCover.hidden = false;
                deckUpNextBlank.hidden = true;
            } else {
                deckUpNextCover.removeAttribute('src');
                deckUpNextCover.hidden = true;
                deckUpNextBlank.hidden = false;
            }
        }
        deckUpNext.hidden = !next;
        if (!next) return;
        const text = (next.title || 'Unknown title') + (next.artist ? ' — ' + next.artist : '');
        deckUpNextText.textContent = text;
        deckUpNextText.title = text;
    }

    // Position now: the polled position, moved on by the time since the poll while playing.
    function deckPosition() {
        if (!deckTrack || !deckAnchor) return 0;
        const moved = deckPlaying === true ? performance.now() - deckAnchor.at : 0;
        const pos = deckAnchor.progress_ms + moved;
        return Math.min(Math.max(0, pos), deckTrack.duration_ms || pos);
    }

    async function pollPlayback() {
        const res = await fetch('/admin/playback?key=' + authKey);
        if (!res.ok) return;
        const play = await res.json();
        deckPlaying = play.playing === true ? true : play.playing === false ? false : null;
        deckTrack = play.track || null;
        deckAnchor = deckTrack ? {progress_ms: deckTrack.progress_ms, at: performance.now()} : null;
        renderTrack(deckTrack);
        renderUpNext(play.next || null);
        renderPlayState();
    }

    async function loadSettings() {
        const res = await fetch('/admin/settings?key=' + authKey);
        if (!res.ok) return;
        const settings = await res.json();
        renderAutomate(settings.automate !== 'false');
    }

    async function playerControl(action) {
        const res = await fetch('/admin/player/' + action + '?key=' + authKey, {method: 'POST'});
        if (!res.ok) {
            const d = await res.json().catch(() => ({}));
            alert(d.detail || 'Playback command failed');
        }
        setTimeout(pollPlayback, 300);
    }

    document.getElementById('deckPrev').addEventListener('click', () => playerControl('prev'));
    document.getElementById('deckNext').addEventListener('click', () => playerControl('next'));
    deckPlay.addEventListener('click', () => playerControl('playpause'));

    automateButton.addEventListener('click', async function() {
        const res = await fetch('/admin/automate?key=' + authKey, {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({enabled: this.getAttribute('aria-pressed') !== 'true'})
        });
        if (!res.ok) {
            const d = await res.json().catch(() => ({}));
            alert(d.detail || 'Failed to change automate');
        }
        loadSettings();
    });

    function frame(now) {
        deckFill.style.width = deckTrack && deckTrack.duration_ms
            ? (deckPosition() / deckTrack.duration_ms * 100).toFixed(2) + '%' : '0%';
        const second = Math.floor(deckPosition() / 1000);
        if (second !== deckShownSecond) {
            deckShownSecond = second;
            deckElapsed.textContent = fmtTime(deckPosition());
        }
        requestAnimationFrame(frame);
    }

    function applyVisibility() {
        if (document.visibilityState === 'visible') pollPlayback();
    }

    document.addEventListener('visibilitychange', applyVisibility);
    setInterval(() => { if (document.visibilityState === 'visible') pollPlayback(); }, 1000);
    setInterval(() => { if (document.visibilityState === 'visible') loadSettings(); }, 5000);
    loadSettings();
    requestAnimationFrame(frame);
    applyVisibility();

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


# The audio monitor and studio drawn in the admin panel. Their scripts and styles live here;
# their data comes from the audio container's control API, which is reachable only over the
# Compose network. Only admins reach either, and only these control paths, by method.
AUDIO_MONITOR_FILES = {
    "monitor.js": ("audio-monitor.js", "text/javascript; charset=utf-8"),
    "monitor.css": ("audio-monitor.css", "text/css; charset=utf-8"),
    "studio.js": ("studio.js", "text/javascript; charset=utf-8"),
    "studio.css": ("studio.css", "text/css; charset=utf-8"),
}
STATIC_DIR = Path(__file__).resolve().parent / "static"
AUDIO_CONTROL_ROUTES = {
    "GET": {"api/state", "api/graph", "api/meters"},
    "POST": {"api/bypass", "api/agc", "api/graph", "api/graph/reload"},
}
# Routes whose body is relayed as it arrives, with no read timeout, and their media types.
AUDIO_STREAM_ROUTES = {
    "api/meters": "text/event-stream",
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
    if path in AUDIO_STREAM_ROUTES:
        # Meters (server-sent events): relay the stream as it arrives.
        client = http_client("audio")
        # No read timeout: the stream stays open for as long as the viewer listens or watches.
        stream_timeout = httpx.Timeout(10, read=None)
        try:
            upstream = await client.send(
                client.build_request("GET", url, timeout=stream_timeout), stream=True
            )
        except httpx.HTTPError:
            return JSONResponse({"error": "audio control is unreachable"}, status_code=502)

        async def relay():
            try:
                async for chunk in upstream.aiter_raw():
                    yield chunk
            finally:
                # Only the upstream response is closed; the shared client stays open.
                await upstream.aclose()

        return StreamingResponse(
            relay(),
            status_code=upstream.status_code,
            media_type=AUDIO_STREAM_ROUTES[path],
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )
    headers = {}
    body = None
    if request.method == "POST":
        body = await request.body()
        headers["Content-Type"] = request.headers.get("content-type", "application/json")
    try:
        # Applying a graph waits for the new processor to warm up and take over (30 s client timeout).
        upstream = await http_client("audio").request(
            request.method, url, content=body, headers=headers
        )
    except httpx.HTTPError:
        return JSONResponse({"error": "audio control is unreachable"}, status_code=502)
    return Response(
        upstream.content,
        status_code=upstream.status_code,
        media_type=upstream.headers.get("content-type"),
        headers={"Cache-Control": "no-store"},
    )


# Studio audio rides WebSockets. The microphone (s16le mono 44.1 kHz) is relayed into the
# control API's POST /api/mic; the listen feed (s16le stereo 44.1 kHz) is relayed out of its
# GET /api/listen. Each backlog holds 0.5 s; past that the oldest audio is dropped, so a slow
# peer costs audio instead of adding latency.
MIC_MAX_BACKLOG_BYTES = 44100
MIC_UPSTREAM_TIMEOUT = 10
LISTEN_MAX_BACKLOG_BYTES = 88200
LISTEN_MESSAGE_BYTES = 3528  # 20 ms of stereo 16-bit 44.1 kHz; a multiple of the 4-byte frame
LISTEN_DRAIN_TIMEOUT = 2


class ByteBacklog:
    """Byte-bounded FIFO of audio chunks, read as the body of one upstream request or as WebSocket messages."""

    def __init__(self, max_bytes: int):
        self.max_bytes = max_bytes
        self.chunks: deque = deque()
        self.size = 0
        self.closed = False
        self.ready = asyncio.Event()

    def put(self, chunk: bytes) -> None:
        self.chunks.append(chunk)
        self.size += len(chunk)
        # Keep the newest chunk even if it alone is over the limit.
        while self.size > self.max_bytes and len(self.chunks) > 1:
            self.size -= len(self.chunks.popleft())
        self.ready.set()

    def close(self) -> None:
        self.closed = True
        self.ready.set()

    async def get(self) -> Optional[bytes]:
        while not self.chunks:
            if self.closed:
                return None
            self.ready.clear()
            await self.ready.wait()
        chunk = self.chunks.popleft()
        self.size -= len(chunk)
        return chunk

    async def body(self):
        while (chunk := await self.get()) is not None:
            yield chunk


def is_admin_websocket(websocket: WebSocket) -> bool:
    """require_admin for a WebSocket handshake, taking the key from the same places."""
    key = (
        websocket.headers.get("X-Auth-Key")
        or websocket.query_params.get("key")
        or websocket.cookies.get("admin_key")
    )
    return key == AUTH_KEY


def is_same_origin_websocket(websocket: WebSocket) -> bool:
    """Only the API's own origin may open the mic. Browsers always send Origin on a WebSocket,
    and a page on another site would otherwise ride on the admin cookie. Only the host is
    compared, so this holds whether or not a TLS-terminating proxy forwards the scheme."""
    origin = websocket.headers.get("origin")
    host = websocket.headers.get("host")
    if not origin or not host:
        return False
    return urlsplit(origin).netloc.lower() == host.lower()


async def relay_mic(backlog: ByteBacklog) -> Optional[str]:
    """Send the backlog to the control API as one chunked request. Returns why the session
    ended early, or None when the body ended normally (a newer session replaced this one)."""
    try:
        upstream = await http_client("audio").post(
            f"{AUDIO_CONTROL_URL}/api/mic",
            content=backlog.body(),
            headers={"Content-Type": "application/octet-stream"},
        )
    except httpx.ConnectError:
        return "audio control is unreachable"
    except httpx.HTTPError:
        # The control API can answer and close while the body is still being written.
        return "audio control closed the mic connection"
    if upstream.status_code == 404:
        return "mic input is disabled"
    if upstream.status_code != 200:
        return f"audio control returned {upstream.status_code}"
    return None


async def receive_mic(websocket: WebSocket, backlog: ByteBacklog) -> None:
    while True:
        message = await websocket.receive()
        if message["type"] == "websocket.disconnect":
            return
        if message.get("bytes"):
            backlog.put(message["bytes"])


@app.websocket("/admin/audio/mic")
async def admin_audio_mic(websocket: WebSocket):
    if not (is_same_origin_websocket(websocket) and is_admin_websocket(websocket)):
        # Closing before accept refuses the handshake; the browser sees a failed connection.
        await websocket.close(code=1008, reason="Forbidden")
        return
    await websocket.accept()
    backlog = ByteBacklog(MIC_MAX_BACKLOG_BYTES)
    upstream = asyncio.create_task(relay_mic(backlog))
    receiver = asyncio.create_task(receive_mic(websocket, backlog))
    await asyncio.wait({upstream, receiver}, return_when=asyncio.FIRST_COMPLETED)
    if receiver.done():
        # The studio closed the socket: end the upstream body and let the request finish.
        backlog.close()
        await asyncio.wait({upstream}, timeout=MIC_UPSTREAM_TIMEOUT)
        upstream.cancel()
        await asyncio.gather(upstream, return_exceptions=True)
        return
    # The upstream session ended first: mic disabled, unreachable, or replaced.
    receiver.cancel()
    backlog.close()
    reason = upstream.result()
    try:
        if reason is None:
            await websocket.close(code=1000, reason="mic session replaced")
        else:
            await websocket.close(code=1011, reason=reason)
    except (RuntimeError, WebSocketDisconnect):
        pass


async def read_listen(upstream: httpx.Response, backlog: ByteBacklog) -> Optional[str]:
    """Cut the upstream PCM into whole 20 ms messages. A partial message waits for the next chunk,
    so every message starts on a frame boundary. Returns why the feed failed, or None at the end."""
    pending = b""
    try:
        async for chunk in upstream.aiter_raw():
            data = pending + chunk
            usable = len(data) - len(data) % LISTEN_MESSAGE_BYTES
            for start in range(0, usable, LISTEN_MESSAGE_BYTES):
                backlog.put(data[start:start + LISTEN_MESSAGE_BYTES])
            pending = data[usable:]
    except httpx.HTTPError:
        return "audio control closed the listen feed"
    return None


async def send_listen(websocket: WebSocket, backlog: ByteBacklog) -> None:
    async for chunk in backlog.body():
        await websocket.send_bytes(chunk)


async def watch_disconnect(websocket: WebSocket) -> None:
    """The listener sends nothing; this only notices when the socket goes away."""
    while True:
        message = await websocket.receive()
        if message["type"] == "websocket.disconnect":
            return


async def close_socket(websocket: WebSocket, code: int, reason: str) -> None:
    try:
        await websocket.close(code=code, reason=reason)
    except (RuntimeError, WebSocketDisconnect):
        pass


@app.websocket("/admin/audio/listen")
async def admin_audio_listen(websocket: WebSocket):
    if not (is_same_origin_websocket(websocket) and is_admin_websocket(websocket)):
        await websocket.close(code=1008, reason="Forbidden")
        return
    await websocket.accept()
    client = http_client("audio")
    try:
        # No read timeout: the feed stays open for as long as the studio listens.
        upstream = await client.send(
            client.build_request("GET", f"{AUDIO_CONTROL_URL}/api/listen", timeout=httpx.Timeout(10, read=None)),
            stream=True,
        )
    except httpx.HTTPError:
        await close_socket(websocket, 1011, "audio control is unreachable")
        return
    if upstream.status_code != 200:
        await upstream.aclose()
        await close_socket(websocket, 1011, f"audio control returned {upstream.status_code}")
        return

    backlog = ByteBacklog(LISTEN_MAX_BACKLOG_BYTES)
    reader = asyncio.create_task(read_listen(upstream, backlog))
    sender = asyncio.create_task(send_listen(websocket, backlog))
    watcher = asyncio.create_task(watch_disconnect(websocket))
    tasks = {reader, sender, watcher}
    error = None
    try:
        finished, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        if reader in finished:
            error = reader.result()
            if error is None:
                # The feed ended cleanly: send what is still queued, then close.
                backlog.close()
                await asyncio.wait({sender}, timeout=LISTEN_DRAIN_TIMEOUT)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await upstream.aclose()
    if reader in finished:
        # The upstream ended first: 1011 with the reason on failure, 1000 on a clean end.
        if error is None:
            await close_socket(websocket, 1000, "listen feed ended")
        else:
            await close_socket(websocket, 1011, error)


@app.get("/admin", response_class=HTMLResponse)
async def admin_panel(request: Request):
    return admin_page_response(request, ADMIN_HTML)


@app.get("/admin/requests")
async def admin_get_requests(_: None = Depends(require_admin)):
    return await asyncio.to_thread(list_admin_requests)


@app.get("/admin/requests/deleted")
async def admin_get_deleted_requests(_: None = Depends(require_admin)):
    return await asyncio.to_thread(list_deleted_requests)


@app.post("/admin/requests/{request_id}/approve")
async def admin_approve_request(request_id: int, _: None = Depends(require_admin)):
    async with request_moderation_lock:
        try:
            row = await asyncio.to_thread(get_request, request_id)
            if not row:
                raise HTTPException(status_code=404, detail="Request not found")
            if row["status"] != "pending":
                raise HTTPException(status_code=409, detail="Request is no longer pending")
            if await asyncio.to_thread(is_song_banned, row["spotify_uri"]):
                raise HTTPException(status_code=409, detail="This song is banned")
            await librespot.add_to_queue(row["spotify_uri"])
            await refresh_queue_now(librespot)
            await asyncio.to_thread(set_request_status, request_id, "approved")
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))
    return {"success": True}


class RequestMovePayload(BaseModel):
    direction: Literal["up", "down"]


@app.post("/admin/requests/{request_id}/move")
async def admin_move_request(request_id: int, payload: RequestMovePayload, _: None = Depends(require_admin)):
    async with request_moderation_lock:
        outcome = await asyncio.to_thread(move_pending_request, request_id, payload.direction)
    if outcome == "missing":
        raise HTTPException(status_code=404, detail="Request not found")
    if outcome == "wrong_status":
        raise HTTPException(status_code=409, detail="Only pending requests can be moved")
    if outcome == "boundary":
        raise HTTPException(status_code=409, detail="Request is already at that end of the queue")
    return {"success": True}


@app.delete("/admin/requests/{request_id}")
async def admin_delete_request(request_id: int, _: None = Depends(require_admin)):
    async with request_moderation_lock:
        outcome = await asyncio.to_thread(delete_pending_request, request_id)
    if outcome == "missing":
        raise HTTPException(status_code=404, detail="Request not found")
    if outcome == "wrong_status":
        raise HTTPException(status_code=409, detail="Only pending requests can be deleted")
    return {"success": True}


@app.post("/admin/requests/{request_id}/restore")
async def admin_restore_request(request_id: int, _: None = Depends(require_admin)):
    async with request_moderation_lock:
        outcome = await asyncio.to_thread(restore_deleted_request, request_id)
    if outcome == "missing":
        raise HTTPException(status_code=404, detail="Request not found")
    if outcome == "wrong_status":
        raise HTTPException(status_code=409, detail="Only deleted requests can be restored")
    if outcome == "song_banned":
        raise HTTPException(status_code=409, detail="This song is banned and cannot be restored")
    return {"success": True}


@app.get("/admin/listeners")
async def admin_get_listeners(_: None = Depends(require_admin)):
    # Collect all UUIDs that have voted to skip the current song
    current_voters: set[str] = set()
    for voters in vote_skips.values():
        for entry in voters:
            uuid = entry.split(":")[0]
            current_voters.add(uuid)

    bans = await asyncio.to_thread(active_bans)
    result = []
    for uuid, data in listeners.items():
        result.append({
            **data,
            "voted_skip": uuid in current_voters,
            "banned": ban_matches(bans, data.get("ip"), uuid),
        })
    return result


@app.get("/admin/settings")
async def admin_get_settings(_: None = Depends(require_admin)):
    value = await asyncio.to_thread(read_auto_approve)
    automate = await asyncio.to_thread(read_automate)
    return {
        "auto_approve": value if value is not None else "false",
        "automate": "true" if automate else "false",
    }


class AutoApprovePayload(BaseModel):
    enabled: bool


@app.post("/admin/auto-approve")
async def admin_set_auto_approve(payload: AutoApprovePayload, _: None = Depends(require_admin)):
    value = "true" if payload.enabled else "false"
    await asyncio.to_thread(write_auto_approve, value)
    return {"success": True, "auto_approve": value}


class AutomatePayload(BaseModel):
    enabled: bool


@app.post("/admin/automate")
async def admin_set_automate(payload: AutomatePayload, _: None = Depends(require_admin)):
    try:
        await automate_controller.set_enabled(payload.enabled)
    except ValueError as e:
        raise HTTPException(status_code=502, detail=str(e))
    return {"success": True, "automate": "true" if payload.enabled else "false"}


PLAYER_ACTIONS = ("prev", "playpause", "next")


@app.post("/admin/player/{action}")
async def admin_player_control(action: str, _: None = Depends(require_admin)):
    if action not in PLAYER_ACTIONS:
        raise HTTPException(status_code=400, detail="Unknown playback action")
    try:
        await automate_controller.player_action(action)
    except ValueError as e:
        raise HTTPException(status_code=502, detail=str(e))
    return {"success": True}


def _admin_track_info(current: dict) -> dict:
    return {
        "songid": current["songid"],
        "title": current.get("song", ""),
        "artist": ", ".join(a.get("name", "") for a in current.get("artist") or []),
        "album": current.get("album", ""),
        "cover": current.get("cover", ""),
    }


def _admin_playback_snapshot() -> Optional[dict]:
    """The poller's latest snapshot while it is fresh enough to show, else None."""
    snapshot = latest_snapshot
    if snapshot["monotonic"] is None:
        return None
    if time.monotonic() - snapshot["monotonic"] > REALTIME_SNAPSHOT_MAX_AGE:
        return None
    return snapshot


def _admin_track() -> Optional[dict]:
    """The poller's latest undelayed track, with its position moved on to now while playing.

    None when nothing is playing or the poller's read is too old to trust.
    """
    snapshot = _admin_playback_snapshot()
    current = snapshot["current"] if snapshot else None
    if not current or not current.get("songid"):
        return None
    progress_ms = current.get("progress_ms", 0)
    if current.get("playing"):
        progress_ms += (time.monotonic() - snapshot["monotonic"]) * 1000
    return {
        **_admin_track_info(current),
        "progress_ms": int(progress_ms),
        "duration_ms": current.get("duration_ms", 0),
    }


def _admin_next_track() -> Optional[dict]:
    """The track go-librespot has queued next, from the same poll as the current track."""
    snapshot = _admin_playback_snapshot()
    queue = snapshot["queue"] if snapshot else []
    if not queue or not queue[0].get("songid"):
        return None
    return _admin_track_info(queue[0])


@app.get("/admin/playback")
async def admin_get_playback(_: None = Depends(require_admin)):
    # Undelayed Spotify state from the poller: playing is true, false, or null before its first
    # poll. track is the song on the deck (paused included), or null when nothing is loaded.
    # next is the song queued after it, or null.
    return {"playing": spotify_playing["value"], "track": _admin_track(), "next": _admin_next_track()}

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
    return await asyncio.to_thread(list_bans)


@app.post("/admin/bans")
async def admin_add_ban(payload: BanPayload, _: None = Depends(require_admin)):
    # IPv6 bans are widened to the household's /64 (see normalize_ban_ip)
    ip = normalize_ban_ip(payload.ip)
    uuid = (payload.uuid or "").strip()
    if not ip and not uuid:
        raise HTTPException(status_code=400, detail="Provide an IP and/or a listener ID to ban")
    minutes = payload.duration_minutes or 0
    await asyncio.to_thread(add_ban, ip, uuid, (payload.reason or "").strip(), minutes)
    return {"success": True}


@app.delete("/admin/bans/{ban_id}")
async def admin_delete_ban(ban_id: int, _: None = Depends(require_admin)):
    await asyncio.to_thread(delete_ban, ban_id)
    return {"success": True}


class SongBanPayload(BaseModel):
    songid: str
    track_name: Optional[str] = None
    artist_name: Optional[str] = None


@app.get("/admin/song-bans")
async def admin_get_song_bans(_: None = Depends(require_admin)):
    return await asyncio.to_thread(list_song_bans)


@app.post("/admin/song-bans")
async def admin_add_song_ban(payload: SongBanPayload, _: None = Depends(require_admin)):
    sid = normalize_songid(payload.songid)
    if not sid:
        raise HTTPException(status_code=400, detail="No song id provided")
    await asyncio.to_thread(
        add_song_ban, sid, (payload.track_name or "").strip(), (payload.artist_name or "").strip()
    )
    return {"success": True}


@app.delete("/admin/song-bans/{ban_id}")
async def admin_delete_song_ban(ban_id: int, _: None = Depends(require_admin)):
    await asyncio.to_thread(delete_song_ban, ban_id)
    return {"success": True}


@app.get("/admin/bans/view", response_class=HTMLResponse)
async def bans_page(request: Request):
    return admin_page_response(request, BANS_HTML)
