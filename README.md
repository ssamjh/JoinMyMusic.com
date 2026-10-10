# Music-Sync-Web

The listening and song-request site at `joinmymusic.com`, plus the services behind it.

## Layout

- `frontend/` — Astro + Tailwind static site (the page at `joinmymusic.com`)
- `backend/docker-compose.yml` — one Compose file that runs both backend containers
- `backend/api/` — FastAPI app + Dockerfile (the API at `api.joinmymusic.com`)
- `backend/audio/` — JMM Audio: headless Spotify Connect player, radio processing and
  AAC streamer, with its own Dockerfile

The API and audio services are two separate containers (`api` and `audio`) in one
Compose stack.

```text
listeners --> joinmymusic.com (frontend) --SSE/JSON--> api.joinmymusic.com (backend/api)
                     |                                         |
                     | plays HLS / Icecast                     | go-librespot API (now playing, queue, skip)
                     v                                         v
           streaming servers <--RTMP + Icecast AAC-- JMM Audio (backend/audio)
                                                   Spotify Connect device "JMM Audio"
```

## Frontend

Astro 7 with Tailwind 4 (via `@tailwindcss/vite`), building to a static site. In
production it talks to the backend at `api.joinmymusic.com`. During local
development, Astro proxies browser `/api` requests to that live API (dropping the prefix), so the
browser stays same-origin and does not need a CORS exception. The API still
must be reachable for local frontend testing.

```bash
cd frontend
npm install
npm run dev      # local dev server
npm run build    # static output in frontend/dist/
npm run check    # astro/TypeScript diagnostics
```

Open the URL printed by `npm run dev` (usually `http://localhost:4321`). API
requests and the SSE feed are forwarded to `https://api.joinmymusic.com`; the
static production build continues to call that API directly.

Deploy `frontend/dist/` to any static host.

### How the styling is split

- `src/styles/global.css` — Tailwind entry. Holds the theme tokens (aliased with
  `@theme inline` so utilities follow the runtime light/dark class on `<body>`)
  plus the component CSS that is not expressible as utilities: the 3D turntable,
  the pressed-record label, the audio-reactive canvases, and the state classes
  `app.js` toggles (`.show`, `.active`, `.is-playing`, `.flip-swap`, …).
- Everything else — layout, spacing, typography, colour — is Tailwind utilities
  in the `.astro` components.

`public/js/app.js` is the application logic: a classic (non-module) script
that owns playback, the SSE feed, the visualiser and the record animation. It is
loaded with `is:inline` because the inline `onclick` handlers and Cloudflare
Turnstile's `onload=onTurnstileLoad` callback need its functions on `window`.

### Kiosk / display modes

Query flags read by `app.js`: `/?display` (visuals only, no controls),
`&noaudio`, `&volume=0-100`, and `/?zoom=1.2`.

## Backend (`backend/`)

Both containers are configured in the `environment` sections of
`backend/docker-compose.yml`. Fill those in, then on an x86-64 (amd64) Docker host:

```bash
cd backend
docker compose up -d --build          # both containers
docker compose up -d --build api      # or just one: api / audio
docker compose logs -f audio          # first start prints a Spotify pairing URL and code
```

### API (`backend/api/`)

Visit `/setup` once to authorise Spotify. The SQLite database and Spotify token
cache are stored in `backend/data/` (mounted at `/data`).

There is no automated API test suite. For API changes, verify imports and startup with
suitable environment values, then exercise the affected endpoint or SSE flow.

Now-playing, skip and add-to-queue go to the audio container's go-librespot API at
`LIBRESPOT_API_URL` (`http://audio:3678` by default), so the `audio` service must be
running. Search and track lookups use the Spotify Web API. The displayed upcoming
song comes from go-librespot's `next_track` status field.

| Endpoint | Returns |
|---|---|
| `GET /events` | SSE: `metadata`, `listeners`, `history`, `queue`, `skipvotes`, `stop`, `announce`, `preferences` (music metadata delayed to match the audio) |
| `GET /events-realtime` | Same events, `metadata` and `queue` undelayed; filter with `?music=1` etc. |
| `GET /metadata`, `GET /history`, `GET /queue` | Current song, play history, and the next song |
| `POST /listener` | Listener heartbeat, including `theme` (`light`/`dark`) and `effects` (boolean) |
| `POST /admin/listener/preferences` | Authenticated per-listener theme/FX command delivered by the `preferences` SSE event |

Metadata includes `deck_side` (`left` or `right`), assigned by the API on song
changes. Both metadata feeds and reconnect snapshots use this value so browsers
agree on the active deck. Listener preferences are visible and controllable in
the admin panel; IP addresses are blurred until hovered or focused.

The `queue` event keeps the `{"queue": [...]}` shape: `queue` is empty when no next
song is known, or contains a single track. The API polls go-librespot status and sends
an update when that next track changes, including after a request is added. The audio
service enables go-librespot metadata caching so `/status` can provide `next_track`;
whole-context metadata sweeping remains disabled. After changing
`backend/audio/config/spotify.yml`, restart the audio service with
`docker compose restart audio` from `backend/`.

### Audio (`backend/audio/`)

A headless Spotify Connect player that processes the audio like a radio station and
streams it out. No sound card, desktop or browser is involved.

```text
Spotify Connect --> go-librespot --> supervisor --> Liquidsoap --> bridge --> processor --> bridge --> encoder --> RTMP server
                                                       ^         (FFmpeg,                    +--> AAC / ADTS --> Icecast server
                              optional VBAN input -----+          replaceable)               +--> optional PCM / VBAN output
```

**Requirements:** an x86-64 (amd64) Docker host, because the image uses prebuilt amd64
binaries. You also need a Spotify Premium account and existing RTMP and Icecast
servers. The RTMP server must accept audio-only FLV.

**Setup**

1. Set `RTMP_URL` and `ICECAST_URL` in the `audio` service of
   `backend/docker-compose.yml`. Both are required. Percent-encode special characters
   in the Icecast password.
2. Start it from `backend/` with `docker compose up -d --build audio`.
3. Run `docker compose logs -f audio`. On first start it prints a Spotify pairing URL
   and code. Open the URL, sign in, and enter the code if asked.
4. Choose **JMM Audio** in Spotify's Connect device picker.

The login is saved in the `spotify-state` volume. `docker compose down -v` erases it.

**Settings**

| Setting | Where |
|---|---|
| `RTMP_URL`, `ICECAST_URL` (required) | `audio` environment in `backend/docker-compose.yml` |
| `STREAM_BITRATE` (default `256k`) | same |
| `RADIO_FILTER`: an extra FFmpeg filter chain, applied by the encoder after processing | same |
| `FFMPEG_LOGLEVEL` (default `warning`) | same |
| `CONTROL_PORT` (default `8090`, `0` disables) | same |
| Device name, crossfade (5 s), autoplay, login method, volume (off) | `backend/audio/config/spotify.yml` |
| VBAN output and input, Spotify input trim | `backend/audio/config/audio.yml` |
| Input AGC and VBAN trim | `backend/audio/config/processing.liq` |
| Processing chain | `backend/audio/config/processing.ffmpeg` (applied live) |

The `config/` directory is mounted read-only. Saving `processing.ffmpeg` applies it
live. After editing `processing.liq`, `audio.yml` or `spotify.yml`, run
`docker compose restart audio`. After changing environment values run
`docker compose up -d audio`.

**Processing**

- Liquidsoap is the input stage. It trims Spotify (`spotify.trim_db` in `audio.yml`,
  default -12 dB), mixes in the optional VBAN input (`vban_receiver.trim_db`, default
  -6 dB; Spotify is ducked by `vban_receiver.duck_db`, default -10 dB, while the input is
  live), and applies the input AGC: a gated leveler that moves its gain at fixed
  dB-per-second rates toward a target RMS level. It won't raise the gain for quiet
  material and holds it during silence, so quiet passages and pauses aren't pumped up.
  Its rates, range and thresholds are named settings at the top of the AGC section.
- The processor is an FFmpeg process that runs the five-band broadcast chain from
  `processing.ffmpeg`: noise gate, compressor, EQ, five-band compression and limiting,
  and a final clipper and limiter. It adds back the 12 dB that Liquidsoap removed and
  writes float samples, so nothing clips before the encoder.
- The encoder is a second FFmpeg process that runs for the life of the service. It
  applies `RADIO_FILTER` and the 0.95 safety limiter, then encodes stereo AAC once for
  both servers.
- Liquidsoap sends audio 12 dB low. If you change `link_headroom_db` in `processing.liq`,
  change the matching first stage in `processing.ffmpeg` too.
- The bridge in `scripts/bridge.py` joins these processes. It paces the chain at real
  time and passes each block on as soon as the processor has produced it. A block the
  processor hasn't produced 250 ms after it arrived is played unprocessed instead, and a
  processor that produces nothing for a second is replaced.

**Changing the processing graph**

- Saving `config/processing.ffmpeg`, or applying a graph in the admin monitor, starts a
  new processor. It warms up on the live input for one second, then crossfades to it
  over 20 ms and stops the old one. Spotify, Liquidsoap and both server connections stay
  up, and the output has no gap.
- A graph that FFmpeg rejects is not applied. The running graph stays, and the error is
  logged and shown in the monitor. A replacement that exits during warm-up is dropped the
  same way.
- If the processor crashes or stops producing audio, the output plays the input with
  only the 12 dB makeup (dry) while a new processor starts with the running graph.
  The live values are kept, and the replacement crossfades in.
- Changes made in the admin monitor last until the service restarts.

**Audio monitor (admin panel)**

The API's admin panel has an Audio section, collapsed until opened. Its script and
styles are in the API (`backend/api/static/audio-monitor.js` and `.css`). Its data comes
from the audio container's control API on port 8090, which is not published: the API
reaches it over the Compose network (`AUDIO_CONTROL_URL`, default `http://audio:8090`)
and passes on admin requests at `/admin/audio/`.

- Input and output level meters (peak and RMS). The input is the mix of Spotify and the
  VBAN input after their input trims, before the AGC, so with the AGC and processing
  bypassed it matches the output; the output is the stream as broadcast.
- An AGC gain meter (cut or boost, within the AGC's range), read from Liquidsoap's
  `agc_gain` server command and sent with the meter events as `agc` (dB, `null` when
  unavailable). It is dimmed while the AGC is bypassed.
- **Bypass processing** sends the audio around the processing chain. The radio filter
  and the safety limiter still apply.
- **Bypass AGC** turns off the Liquidsoap input AGC, through Liquidsoap's server socket
  in `/run/audio`. The AGC keeps measuring, so its gain is current when it comes back.
- **Processing graph** shows the source the processor runs, with comments. Apply
  replaces the processor with the edited text. Revert to file runs `processing.ffmpeg`
  again. The monitor marks an edit that differs from the file.

Bypassing the processing needs an FFmpeg build with the `azmq` filter and the `pyzmq`
package, which the image provides. Without them the monitor still shows the meters and
graph editing still works, but processing bypass is disabled.

The control API (the admin proxy passes on only the state, bypass, meter and graph
calls): `GET /api/state`, `POST /api/param`, `POST /api/bypass`, `POST /api/agc`,
`POST /api/reset`, `GET /api/export`, `GET /api/meters` (server-sent events),
`GET /api/graph`, `POST /api/graph` with `{"text": "..."}`, `POST /api/graph/reload`
and `GET /api/source` (the VBAN input level, `{"vban_level_db": ...}`, `null` when the
receiver is off; the API reads it directly).
A graph that FFmpeg rejects returns 400 with the message. A swap already in progress
returns 409, and a replacement that does not take over returns 503.

**Behaviour**

- When playback pauses, silence is streamed so the servers stay connected.
- Each server has its own reconnect loop, retrying every second. One server can stream
  while the other is down. Audio missed during an outage is not replayed.
- If Liquidsoap, the encoder or go-librespot exits, the container exits and Docker
  restarts it. Playback may then need resuming from Spotify or the control API. A
  processor that exits is replaced in place (see above).

**Control API**

go-librespot's API is published on the host at `127.0.0.1:3678`. It has no
authentication, so keep that port mapping on loopback. See the
[API spec](https://github.com/devgianlu/go-librespot/blob/v0.10.2/api-spec.yml).

```sh
curl http://127.0.0.1:3678/status
curl -X POST http://127.0.0.1:3678/player/play   -H 'Content-Type: application/json' -d '{"uri":"spotify:playlist:YOUR_PLAYLIST_ID"}'
curl -X POST http://127.0.0.1:3678/player/pause
curl -X POST http://127.0.0.1:3678/player/next
```

Playback events are on `ws://127.0.0.1:3678/events`.

**VBAN (optional)**

[VBAN](https://github.com/quiniouben/vban) sends uncompressed audio over UDP on the
local network. Both directions are off by default and are configured in
`backend/audio/config/audio.yml`. Stream names can be at most 15 ASCII characters.

- **Output** sends the processed audio as 44.1 kHz, 16-bit stereo PCM (about
  1.4 Mbps) to `ip_address`:`port`.
- **Input** receives a stream, for example a microphone on another machine. It is
  mixed with Spotify at -6 dB, before processing.
  - Only packets from `sender_ip` are accepted.
  - The sender must send 16-bit PCM at 44.1 kHz, the rate of the whole chain. Nothing
    is resampled anywhere.
  - `channels` is `'1,2'` for a stereo sender, or `'1,1'` for mono.
  - **Live input mode:** while Spotify is not playing and the input has audio (RMS
    above -50 dBFS, held for 8 s through pauses), the API sends the SSE event
    `source` with `{"live": true}`. The website then hides the turntable, track
    details and history and shows only the visualizer, in the default orange.
  - `quality` sets the receive buffer: `optimal` (0.1 s), `fast` (0.2 s), `medium`
    (0.4 s, default), `slow` (0.8 s) or `very_slow` (1.5 s). If the input glitches,
    choose a slower setting.
  - Compose publishes UDP port 6980 for it. If you change `port` in `audio.yml`,
    change the `6980:6980/udp` mapping in `backend/docker-compose.yml` to match, and
    open the port in the host's firewall.

**Tests**

The tests need Linux with Python 3, PyYAML, Liquidsoap 2.1.3 and FFmpeg. The image
has all of them. The live-control tests also need `pyzmq` and an FFmpeg build with
`azmq`, and skip when those are missing.

```bash
cd backend/audio
python3 -m unittest discover -s tests -v
```

They cover the supervisor (silence, pauses, shutdown, config validation), the
processing chain (loudness, peak ceiling, latency), both AAC outputs, server outages
and reconnects, the VBAN input mix, the processor swap (continuity, crossfade, failure
handling, real FFmpeg), the graph endpoints and file watching.
