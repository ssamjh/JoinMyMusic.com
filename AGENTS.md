## Model routing

- You own planning, architecture, and final verification. Don't write bulk code yourself.
- Break work into independent, clearly scoped tasks with success criteria and hand each to a subagent.
- Review subagent output before reporting done.

## Project overview

Music-Sync-Web powers the live listening and song-request experience at
`joinmymusic.com`. It consists of a static frontend and a separately deployed API.

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
- `backend/` is a Python FastAPI service served by Uvicorn. It integrates with
  Spotify through Spotipy, exposes public and admin APIs, broadcasts updates with
  Server-Sent Events (SSE), and uses SQLite for persistent data.
- `backend/main.py` defines routes, middleware, auth, and background-task startup;
  `backend/spotify.py` wraps Spotify; `backend/sse.py` handles polling and event
  broadcasts; `backend/storage.py` owns SQLite access and in-memory state.
- Listener presence, skip votes, rate limits, submission IDs, and recent play
  history are ephemeral in-memory state. Requests, settings, and bans use SQLite.

## Important conventions

- Set the frontend API origin with `API_BASE` near the top of
  `frontend/public/js/app.js`. The production API is `api.joinmymusic.com`.
- Keep `app.js` as a classic, non-module script loaded with `is:inline`. Inline
  handlers and Cloudflare Turnstile rely on its callbacks being available on
  `window` before the async Turnstile script runs.
- Prefer Astro components and Tailwind utilities for normal presentation. Keep
  bespoke visual effects and JavaScript-toggled state classes in `global.css`.
- `METADATA_DELAY` intentionally aligns normal SSE metadata with the delayed audio
  stream. `/api/events-realtime` is the undelayed feed for display/control clients.
- Album art is proxied through `/api/cover/{image_id}`. Preserve its constant
  wildcard CORS behavior and immutable-cache assumptions when changing middleware.
- Display modes are controlled by URL parameters: `display`, `noaudio`,
  `volume=0-100`, and `zoom`.
- Backend configuration comes from environment variables. The supported deployment
  values are documented in `backend/docker-compose.yml`; never commit credentials,
  admin keys, token caches, or database contents.

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

Backend development:

```bash
cd backend
python -m venv .venv
pip install -r requirements.txt
uvicorn main:app --reload --host 0.0.0.0 --port 8080
```

Containerized backend:

```bash
cd backend
docker compose up --build
```

The backend expects writable paths for `DB_FILE` and `TOKEN_CACHE_FILE`; Docker
Compose mounts `backend/data` at `/data` for these files.

## Verification

- For frontend changes, run `npm run check` and `npm run build` from `frontend/`.
- There is currently no automated backend test suite. For backend changes, at
  minimum verify imports/startup or run the service with suitable environment
  values, then exercise the affected endpoint or SSE flow.
- Keep frontend/backend contracts in sync when changing event names, payloads,
  routes, CORS behavior, request validation, or configuration.
