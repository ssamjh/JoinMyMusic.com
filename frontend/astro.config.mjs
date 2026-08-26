// @ts-check
import { defineConfig } from 'astro/config';
import tailwindcss from '@tailwindcss/vite';

// Static site — the frontend is hosted on its own origin (Cloudflare Pages) and
// talks to the FastAPI backend cross-origin. See API_BASE in public/js/app.js.
export default defineConfig({
  site: 'https://joinmymusic.com',
  vite: {
    plugins: [tailwindcss()],
  },
});
