import fs from 'node:fs'
import path from 'node:path'
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'
import { VitePWA } from 'vite-plugin-pwa'

// Load self-signed certs for HTTPS (needed for microphone access over LAN)
const certsDir = path.resolve(__dirname, '../certs')
const httpsConfig = fs.existsSync(path.join(certsDir, 'selfsigned.key'))
  ? { key: fs.readFileSync(path.join(certsDir, 'selfsigned.key')), cert: fs.readFileSync(path.join(certsDir, 'selfsigned.crt')) }
  : undefined

// PWA manifest icons must be reachable from Google's WebAPK minting server
// (public internet) — self-hosted Tailscale/LAN URLs aren't, so Android
// falls back to "host first-letter" placeholder. jsDelivr mirrors GitHub
// tagged releases on a public CDN; pin to the current version so each
// release immutably points at its own icons.
const pkg = JSON.parse(fs.readFileSync(path.join(__dirname, 'package.json'), 'utf8'))
const ICON_BASE = `https://cdn.jsdelivr.net/gh/jianpengyao/xylocopa@v${pkg.version}/frontend/public`

// https://vite.dev/config/
export default defineConfig({
  plugins: [
    // Inject __APP_VERSION__ and __BUILD_HASH__ placeholders in index.html.
    // APP_VERSION: apple-touch-icon CDN URL.
    // BUILD_HASH: auto cache-bust — every build produces a new hash so
    // clients clear their stale SW without a manual version bump.
    {
      name: 'inject-build-vars',
      transformIndexHtml(html, ctx) {
        let buildHash = Date.now().toString(36);
        if (ctx.bundle) {
          const entry = Object.keys(ctx.bundle).find(k => k.startsWith('assets/index-') && k.endsWith('.js'));
          if (entry) buildHash = entry.replace(/^assets\/index-|\.js$/g, '');
        }
        return html
          .replace(/__APP_VERSION__/g, pkg.version)
          .replace(/__BUILD_HASH__/g, buildHash)
      },
    },
    // Vite's built-in htmlFallbackMiddleware rewrites ALL 404s to
    // /index.html (SPA fallback) — including missing .js/.css chunks.
    // The browser then receives HTML with Content-Type text/html instead
    // of the expected JS, triggering Safari's "'text/html' is not a valid
    // JavaScript MIME type" error.  This plugin intercepts after the
    // rewrite and returns a proper 404 for asset requests.
    {
      name: 'asset-404',
      configurePreviewServer(server) {
        server.middlewares.use((req, res, next) => {
          req.__originalUrl = req.url;
          next();
        });
        return () => {
          server.middlewares.use((req, res, next) => {
            if (req.__originalUrl && req.url === '/index.html' &&
                /\/assets\/.+\.(js|css|mjs)(\?|$)/.test(req.__originalUrl)) {
              res.writeHead(404, { 'Content-Type': 'text/plain' });
              res.end('Not Found');
              return;
            }
            next();
          });
        };
      },
    },
    react(),
    tailwindcss(),
    VitePWA({
      // autoUpdate (vs prompt): new SW installs + activates without user
      // intervention. Combined with skipWaiting + no NavigationRoute below,
      // tabs see fresh HTML on next navigation without the "zombie SW
      // serving stale cached index.html" failure mode that made cert-regen
      // recovery require website-data clearing.
      registerType: 'autoUpdate',
      devOptions: { enabled: true },
      workbox: {
        skipWaiting: true,
        clientsClaim: true,
        // No 'html' here — index.html is NOT precached. Navigation requests
        // go straight to the network (NetworkOnly route below), so a TLS
        // failure surfaces as Safari's warning page instead of a silent
        // SW-served stale shell.
        globPatterns: ['**/*.{js,css,ico,png,svg,woff2}'],
        // Import existing push notification handler into generated SW
        importScripts: ['/push-handler.js'],
        // Belt-and-suspenders: explicitly tell Workbox NOT to register a
        // default NavigationRoute for SPA fallback (we use NetworkOnly
        // below).  Without this, vite-plugin-pwa would auto-register one
        // that serves precached index.html as the SPA shell.
        navigateFallback: null,
        runtimeCaching: [
          // Navigation requests (top-level page loads) — go straight to
          // network. If TLS / network fails, Safari shows its own warning
          // page or "no internet" UX, which is recoverable. If we cached
          // and served stale HTML here, fetches inside the page would fail
          // silently after cert changes and the user would have to clear
          // website data manually.
          {
            urlPattern: ({ request }) => request.mode === 'navigate',
            handler: 'NetworkOnly',
          },
          // Fluent UI emoji SVGs from jsdelivr — immutable assets, cache forever
          {
            urlPattern: /^https:\/\/cdn\.jsdelivr\.net\/gh\/microsoft\/fluentui-emoji.*\.svg$/,
            handler: 'CacheFirst',
            options: {
              cacheName: 'fluent-emoji-cache',
              expiration: { maxEntries: 300, maxAgeSeconds: 365 * 24 * 60 * 60 },
              cacheableResponse: { statuses: [0, 200] },
            },
          },
          // Thumbnails — cached aggressively (small files, rarely change)
          {
            urlPattern: /\.thumb\.jpg$/,
            handler: 'CacheFirst',
            options: {
              cacheName: 'thumbnail-cache',
              expiration: { maxEntries: 200, maxAgeSeconds: 7 * 24 * 60 * 60 },
            },
          },
          // Image thumbnails — cached aggressively (small JPEG, rarely change)
          {
            urlPattern: /\/api\/thumbs\//,
            handler: 'CacheFirst',
            options: {
              cacheName: 'thumbnail-cache',
              expiration: { maxEntries: 500, maxAgeSeconds: 7 * 24 * 60 * 60 },
            },
          },
          // Files/uploads must bypass SW cache — Safari requires intact
          // HTTP Range responses for <video> playback and Workbox caching
          // strategies strip/corrupt the 206 + Content-Range semantics.
          {
            urlPattern: /\/api\/(?:files|uploads)\//,
            handler: 'NetworkOnly',
          },
          // Polling endpoints refresh every 3-10s — caching adds SW
          // overhead with zero benefit.  NetworkOnly bypasses cache entirely.
          {
            urlPattern: /^.*\/api\/.*/,
            handler: 'NetworkOnly',
          },
          // Catch-all: any request not matched above goes straight to
          // network. Prevents Workbox from returning an empty response
          // (no-response) when a stale SW has outdated routing rules.
          {
            urlPattern: () => true,
            handler: 'NetworkOnly',
          },
        ],
      },
      manifest: {
        name: 'Xylocopa',
        short_name: 'Xylocopa',
        description: 'Multi-agent Claude Code dashboard',
        theme_color: '#06b6d4',
        background_color: '#0a0a0a',
        display: 'standalone',
        orientation: 'portrait',
        start_url: '/',
        icons: [
          { src: `${ICON_BASE}/icon-192.png`, sizes: '192x192', type: 'image/png' },
          { src: `${ICON_BASE}/icon-512.png`, sizes: '512x512', type: 'image/png' },
          { src: `${ICON_BASE}/icon-mask.png`, sizes: '512x512', type: 'image/png', purpose: 'maskable' },
        ],
      },
    }),
  ],
  server: {
    fs: {
      // Allow importing docs/oauth/dropbox/relay.js from tests
      allow: ['..'],
    },
    https: httpsConfig,
    host: '0.0.0.0',
    port: 3000,
    // Vite's built-in CORS middleware answers /api OPTIONS preflights itself
    // (and its default origin allowlist rejects the "null" origin sandboxed
    // preview iframes send), so requests never reach the backend's CORS
    // config. Disable it — the app is same-origin and the backend owns CORS.
    cors: false,
    hmr: {
      // Explicit HMR config stabilises the WebSocket on mobile with self-signed certs
      protocol: httpsConfig ? 'wss' : 'ws',
      port: 3000,
    },
    proxy: {
      // ws:true — port-preview web apps (webapp_present) open WebSockets at
      // /api/preview/.../ws; the shorthand form only proxied plain HTTP,
      // leaving those cards stuck on "connecting".
      '/api': { target: 'http://localhost:8080', ws: true },
      '/ws': { target: 'ws://localhost:8080', ws: true },
    },
  },
  preview: {
    // Prod-build serving mode — no HMR, no ws ping reload loop.
    // Mirrors `server` but without the HMR block.
    https: httpsConfig,
    host: '0.0.0.0',
    port: 3000,
    cors: false,  // same rationale as `server.cors` above
    proxy: {
      // ws:true — port-preview web apps (webapp_present) open WebSockets at
      // /api/preview/.../ws; the shorthand form only proxied plain HTTP,
      // leaving those cards stuck on "connecting".
      '/api': { target: 'http://localhost:8080', ws: true },
      '/ws': { target: 'ws://localhost:8080', ws: true },
    },
  },
  optimizeDeps: {
    // Work around TailwindCSS v4 HMR cache invalidation bug
    exclude: ['@tailwindcss/vite'],
  },
  test: {
    environment: 'jsdom',
    setupFiles: './src/test-setup.js',
  },
})
