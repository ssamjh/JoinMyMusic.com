## Model routing

- You own planning, architecture, and final verification. Don't write bulk code yourself.
- Break work into independent, clearly scoped tasks with success criteria and hand each to a subagent.
- Review subagent output before reporting done.

## Project overview

Music-Sync-Web powers the live listening and song-request experience at
`joinmymusic.com`. It consists of a static frontend and two separately deployed
backend containers: the API and the audio streamer. The two containers are
intentionally separate containers; never merge them. Both are defined as the
`api` and `audio` services in one Compose file, `backend/docker-compose.yml`.

## Architecture and stack

- `frontend/` is an Astro 7 static site styled with Tailwind CSS 4 through
  `@tailwindcss/vite`. It is deployed independently and calls the API cross-origin.
- `frontend/src/pages/index.astro` composes the page from components in
  `frontend/src/components/`; `frontend/src/layouts/BaseLayout.astro` owns the
  document shell and shared metadata.
- `frontend/src/styles/global.css` contains the Tailwind entry point, theme tokens,
  and custom visual/state CSS for the turntable, record, canvases, and animations.
- `frontend/public/js/app.js` is the browser application: audio playback, SSE state,
  visualizers, requests, skip voting, kiosk modes, and Turnstile integration.
- `backend/api/` is a Python FastAPI service served by Uvicorn. It integrates with
  Spotify through Spotipy, exposes public and admin APIs, broadcasts updates with
  Server-Sent Events (SSE), and uses SQLite for persistent data.
- `backend/api/main.py` defines routes, middleware, auth, and background-task startup;
  `backend/api/spotify.py` wraps the Spotify Web API; `backend/api/librespot.py` wraps the
  go-librespot API (now playing, queue, skip); `backend/api/sse.py` handles polling and event
  broadcasts; `backend/api/storage.py` owns SQLite access and in-memory state.
- Listener presence, skip votes, rate limits, submission IDs, recent play history,
  and the upcoming queue are ephemeral in-memory state. Requests, settings, and bans use SQLite.
- `backend/audio/` (JMM Audio) is a headless Spotify Connect player and broadcast
  processor in its own Docker image: go-librespot -> Python supervisor
  (`scripts/run.py`) -> Liquidsoap input stage (`config/processing.liq`) -> bridge
  (`scripts/bridge.py`) -> FFmpeg processor (`config/processing.ffmpeg`, replaceable at
  runtime) -> bridge -> FFmpeg encoder (RADIO_FILTER, safety limiter) -> stereo AAC to an
  RTMP server and an Icecast server (optional VBAN in/out via `config/audio.yml`). It is
  documented in the root `README.md`; there is no separate audio README. Its control API
  (`backend/audio/scripts/control.py`, port 8090, not published) serves meters, the
  bypasses, the processing graph, a raw PCM listen feed of the Spotify input before processing (`/api/listen`) and the
  browser mic input (`/api/mic`, chunked PCM into a FIFO that Liquidsoap mixes with VBAN). The audio monitor UI lives in the API
  (`backend/api/static/audio-monitor.js`/`.css`, scoped under `.jmm-monitor`); the admin
  panel draws it and reaches the control API through an admin-only proxy at
  `/admin/audio/` over the Compose network (`AUDIO_CONTROL_URL`, default
  `http://audio:8090`). The admin "Studio" (`backend/api/static/studio.js`) plays that
  feed over a same-origin WebSocket (`/admin/audio/listen`) and sends the mic over
  `/admin/audio/mic`; the API relays both to the control API. The admin
  "Playback" controls and Automate switch live in `backend/api/automate.py`; with Automate
  off, playback pauses at the end of the current song.
- The API reads now-playing, the next track, skips and queues songs through the audio
  container's go-librespot REST API (`LIBRESPOT_API_URL`, default `http://audio:3678`).
  It polls go-librespot status and exposes the single `next_track` as the queue
  (`{"queue": []}` or `{"queue": [track]}`). `backend/audio/config/spotify.yml` enables
  go-librespot `metadata.enabled` for this status field and keeps `context_sweep`
  disabled. The Spotify Web API is used for search, track lookups and OAuth. The
  frontend plays the downstream HLS/Icecast streams, not the audio container directly.

## Important conventions

- Set the frontend API origin with `API_BASE` near the top of
  `frontend/public/js/app.js`. The production API is `api.joinmymusic.com`.
- Keep `app.js` as a classic, non-module script loaded with `is:inline`. Inline
  handlers and Cloudflare Turnstile rely on its callbacks being available on
  `window` before the async Turnstile script runs.
- Prefer Astro components and Tailwind utilities for normal presentation. Keep
  bespoke visual effects and JavaScript-toggled state classes in `global.css`.
- `METADATA_DELAY` intentionally aligns normal SSE metadata with the delayed audio
  stream. `/events-realtime` is the undelayed feed for display/control clients.
- Album art is proxied through `/cover/{image_id}`. Preserve its constant
  wildcard CORS behavior and immutable-cache assumptions when changing middleware.
- Display modes are controlled by URL parameters: `display`, `noaudio`,
  `volume=0-100`, and `zoom`.
- Backend configuration comes from environment variables. The supported deployment
  values are documented in `backend/docker-compose.yml`; never commit credentials,
  admin keys, token caches, or database contents.
- Audio configuration: `RTMP_URL`, `ICECAST_URL` (required) and `STREAM_BITRATE`
  (default `256k`) are set in the `audio` service's `environment` in
  `backend/docker-compose.yml`. Spotify, VBAN, and
  processing settings live in `backend/audio/config/`, which Compose bind-mounts
  read-only as a directory. Saving `processing.ffmpeg` applies live (the processor is
  replaced, the stream is not restarted); the other files need only
  `docker compose restart audio`.
- The audio image is amd64 only (prebuilt go-librespot binary). It uses normal bridge
  networking: go-librespot binds `0.0.0.0:3678` inside the container, and Compose
  publishes it as `127.0.0.1:3678` only, because it is unauthenticated. Never publish it
  on all interfaces. The audio control API (8090) has no authentication either and is not
  published at all; only the API container reaches it. The VBAN input UDP port in Compose must match
  `vban_receiver.port` in `audio/config/audio.yml`.
- This is a public project: keep docs and comments neutral and current. Don't name
  proprietary or paid software, and don't add history or back story.
- Keep `backend/audio` shell/Python/YAML/Liquidsoap files LF-only (see its
  `.gitattributes`); they run inside Linux containers.

## Common commands

Frontend development and validation:

```bash
cd frontend
npm install
npm run dev
npm run check
npm run build
npm run preview
```

API development:

```bash
cd backend/api
python -m venv .venv
pip install -r requirements.txt
uvicorn main:app --reload --host 0.0.0.0 --port 8080
```

Containerized backend (both containers; x86-64 Docker host for `audio`):

```bash
cd backend
docker compose up -d --build          # api + audio
docker compose up -d --build api      # or one service
docker compose logs -f audio          # first start prints the Spotify pairing URL/code
```

The API expects writable paths for `DB_FILE` and `TOKEN_CACHE_FILE`; Docker
Compose mounts `backend/data` at `/data` for these files.

Audio tests (Linux with Python 3, PyYAML, Liquidsoap 2.1.3, FFmpeg; easiest inside
the image):

```bash
cd backend/audio
python3 -m unittest discover -s tests -v
```

## Verification

- For frontend changes, run `npm run check` and `npm run build` from `frontend/`.
- The API has a stdlib `unittest` suite in `backend/api/tests` (run
  `python -m unittest discover -s tests -v` from `backend/api`); the benchmark is
  `backend/api/tools/bench.py`. For API changes, also verify imports/startup or run
  the service with suitable environment values, then exercise the affected
  endpoint or SSE flow.
- For audio changes, run the `backend/audio/tests` suite on Linux (or in the
  image) and keep the Audio section of `README.md` in sync with `scripts/run.py`
  and `config/`. Processing changes should be measured with `tools/processing_lab.py`.
- Keep frontend/backend contracts in sync when changing event names, payloads,
  routes, CORS behavior, request validation, or configuration.
