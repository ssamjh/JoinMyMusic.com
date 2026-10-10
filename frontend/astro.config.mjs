// @ts-check
import { defineConfig } from 'astro/config';
import tailwindcss from '@tailwindcss/vite';

// Static site — the frontend is hosted on its own origin (Cloudflare Pages) and
// talks to the FastAPI backend cross-origin. See API_BASE in public/js/app.js.
export default defineConfig({
  site: 'https://joinmymusic.com',
  vite: {
    plugins: [tailwindcss()],
    server: {
      // Keep browser requests same-origin during local development. This lets
      // localhost reach the live API without changing its production CORS rules.
      proxy: {
        '/api': {
          target: 'https://api.joinmymusic.com',
          changeOrigin: true,
          // SSE responses stay open for the lifetime of the EventSource.
          timeout: 0,
          proxyTimeout: 0,
        },
      },
    },
  },
});
