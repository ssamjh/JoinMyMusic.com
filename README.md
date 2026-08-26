# Music-Sync-Web

A basic web client to connect to ssamjh's Music Sync backend.

## Layout

- `backend/` — FastAPI app + Dockerfile / docker-compose (the API at `api.joinmymusic.com`)
- `frontend/` — Astro + Tailwind static site (the page at `joinmymusic.com`)

## Frontend

Astro 7 with Tailwind 4 (via `@tailwindcss/vite`), building to a static site. It
is hosted on its own origin and talks to the backend cross-origin — set the
backend URL with `API_BASE` at the top of `public/js/app.js`.

```bash
cd frontend
npm install
npm run dev      # local dev server
npm run build    # static output in frontend/dist/
npm run check    # astro/TypeScript diagnostics
```

Deploy `frontend/dist/` to any static host.

### How the styling is split

- `src/styles/global.css` — Tailwind entry. Holds the theme tokens (aliased with
  `@theme inline` so utilities follow the runtime light/dark class on `<body>`)
  plus the component CSS that is not expressible as utilities: the 3D turntable,
  the pressed-record label, the audio-reactive canvases, and the state classes
  `app.js` toggles (`.show`, `.active`, `.is-playing`, `.flip-swap`, …).
- Everything else — layout, spacing, typography, colour — is Tailwind utilities
  in the `.astro` components.

`public/js/app.js` is unchanged application logic: a classic (non-module) script
that owns playback, the SSE feed, the visualiser and the record animation. It is
loaded with `is:inline` because the inline `onclick` handlers and Cloudflare
Turnstile's `onload=onTurnstileLoad` callback need its functions on `window`.

### Kiosk / display modes

Query flags read by `app.js`: `/?display` (visuals only, no controls),
`&noaudio`, `&volume=0-100`, and `/?zoom=1.2`.
