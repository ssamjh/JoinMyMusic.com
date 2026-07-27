# JoinMyMusic TIDAL bridge

A [TidaLuna](https://github.com/Inrixia/TidaLuna) plugin that gives the JoinMyMusic
backend a control surface for the TIDAL desktop client. It runs inside the client
and exposes a small JSON HTTP API on port **24124**.

TIDAL's public API has no playback-state, queue or skip endpoints, and the desktop
client is not a TIDAL Connect target — so a client mod is the only route to
programmatic queue insertion. The TIDAL client still does all discovery and
playback and still feeds the audio hardware; this only reads state and pushes
tracks into the play queue.

## API

Unauthenticated — it listens only on a secured subnet. No CORS headers are sent,
which is what keeps a random web page from driving the client.

| Method | Path | Returns |
|---|---|---|
| `GET` | `/health` | `{ok, loggedIn}` — `loggedIn` false means TIDAL has no session yet |
| `GET` | `/playback` | `{current: {...}}` in the backend's now-playing shape |
| `GET` | `/search?q=` | `{results: [{id, name, artist, album, cover}]}` (max 10) |
| `GET` | `/track/{id}` | `{song, songid, artist: [{id, name}], album, albumid, cover}` |
| `POST` | `/queue` | body `{"id": "<trackId>"}` → appends to the play queue |
| `POST` | `/next` | skips to the next track |

## Install

1. Install TidaLuna with the [installer](https://github.com/jxnxsdev/TidaLuna-Installer).
   **The desktop installer build of TIDAL only — the Microsoft Store version is not supported.**
2. Build this plugin:
   ```
   pnpm install
   pnpm run build
   ```
3. `pnpm run watch` serves the build on `http://localhost:3000` for live reload;
   add that URL as a plugin store in Luna's settings during development.
4. Set the port under Luna → Settings → JoinMyMusicBridge if 24124 is taken.

### After every TIDAL update — required

TIDAL's auto-updater installs into a **new** `%localappdata%\TIDAL\app-<version>`
directory, so the patched `app/` folder is left behind in the old one and TIDAL
restarts unmodded. The bridge then stops answering and the site loses now-playing,
search, requests and vote-skip.

**Re-run the TidaLuna installer after each TIDAL update.** `/api/auth_status` on the
backend reports `authenticated: false` when the bridge is unreachable, so this
fails loudly rather than silently.

## Notes

- Keep the installed plugin set minimal. In particular do **not** install
  `SongDownloader` or `noTrack` — bulk downloading is the behaviour that actually
  gets TIDAL accounts flagged.
- TidaLuna is beta with no stable API contract; `@luna/lib` is refactored often.
  Pin the Luna version and expect to fix this plugin on upgrades.
- Modding the client is against TIDAL's terms of service.
