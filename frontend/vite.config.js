import fs from 'node:fs'
import path from 'node:path'
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'
import { VitePWA } from 'vite-plugin-pwa'
import { runtimeCaching } from './sw-runtime-caching.js'
import { proxyBypass, redactUrl } from './proxy-bypass.js'

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
    // index.html headers + a small access log for the prod (preview) server.
    // Must stay after 'asset-404' so a missing chunk still gets its 404.
    //  - index.html: Cache-Control no-store and no ETag. Vite serves it
    //    no-cache with a weak ETag and answers 304 to a matching
    //    If-None-Match — a truncated copy WebKit cached (bug 312248) would
    //    be revalidated as "fresh" until website data is cleared. It is
    //    ~11 KB; not caching it costs nothing.
    //  - while dist/ is being rebuilt in place (the post-commit hook runs
    //    `vite build` under the live server) index.html is briefly missing
    //    and Vite answered an empty 404 — a blank page with no way out in a
    //    standalone PWA. Answer 503 with a page that reloads itself instead.
    //  - access log for page loads (incl. those the SW re-fetches, which
    //    arrive as Sec-Fetch-Dest: empty), sw.js and the recovery probes,
    //    with the client address: behind the proxy the backend only sees
    //    127.0.0.1, so "did the phone reach the server at all?" had no
    //    answer in the logs.
    {
      name: 'preview-index-html',
      configurePreviewServer(server) {
        const distDir = path.resolve(server.config.root, server.config.build.outDir)
        const REBUILDING_HTML = '<!doctype html><meta charset="utf-8">' +
          '<meta name="viewport" content="width=device-width, initial-scale=1">' +
          '<title>Xylocopa</title><style>:root{color-scheme:light dark}' +
          'body{font:15px -apple-system,BlinkMacSystemFont,sans-serif;text-align:center;margin-top:40vh}</style>' +
          '<p>Updating\u2026 reloading in a moment.</p>' +
          '<script>setTimeout(function(){location.reload()},2000)</script>'
        const serveIndex = (req, res) => {
          fs.readFile(path.join(distDir, 'index.html'), (err, body) => {
            if (err) {
              res.writeHead(503, { 'Content-Type': 'text/html; charset=utf-8', 'Cache-Control': 'no-store', 'Retry-After': '2' })
              res.end(req.method === 'HEAD' ? undefined : REBUILDING_HTML)
              return
            }
            res.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8', 'Cache-Control': 'no-store' })
            res.end(req.method === 'HEAD' ? undefined : body)
          })
        }
        const pathOf = (req) => (req.url || '').split('?')[0]
        const isIndexRequest = (req) =>
          pathOf(req) === '/index.html' && (req.method === 'GET' || req.method === 'HEAD')
        const shouldLog = (req, url) =>
          (req.headers.accept || '').startsWith('text/html') || url === '/sw.js' ||
          /[?&](src=offline|_xs=)/.test(req.url || '')
        server.middlewares.use((req, res, next) => {
          if (shouldLog(req, pathOf(req))) {
            const t0 = Date.now()
            // Read now: over HTTP/2 the socket proxy is gone by 'finish'.
            const ip = (req.socket?.remoteAddress || '?').replace(/^::ffff:/, '')
            // Media ?token= / WS ?ticket= / preview + probe path capabilities
            // never reach the log.
            const url = redactUrl(req.originalUrl || req.url)
            let logged = false
            // 'close' too: an aborted request (the probes' 8s abort) never
            // emits 'finish'.
            const log = () => {
              if (logged) return
              logged = true
              const status = res.writableEnded ? res.statusCode : `${res.statusCode}-aborted`
              const ua = (req.headers['user-agent'] || '').slice(0, 120)
              console.log(`[access] ${new Date().toISOString()} ${ip} ${req.method} ${url} ${status} ${Date.now() - t0}ms "${ua}"`)
            }
            res.on('finish', log)
            res.on('close', log)
          }
          if (isIndexRequest(req)) return serveIndex(req, res)
          next()
        })
        // Connection log for non-loopback clients: TCP accept, TLS result,
        // close. With no [access] line from the phone the tunnel still
        // carried traffic both ways, so "packets never arrived", "TLS was
        // rejected" (untrusted root, client abort) and "connected but never
        // sent a request" could not be told apart.
        const httpServer = server.httpServer
        if (httpServer) {
          const now = () => new Date().toISOString()
          const peerOf = (sock) => `${(sock.remoteAddress || '?').replace(/^::ffff:/, '')}:${sock.remotePort}`
          const isLoopback = (sock) => /^(::ffff:)?127\.|^::1$/.test(sock.remoteAddress || '')
          httpServer.on('connection', (sock) => {
            if (isLoopback(sock)) return
            const t0 = Date.now()
            const peer = peerOf(sock)
            console.log(`[conn] ${now()} ${peer} tcp-open`)
            sock.on('close', (hadError) => {
              console.log(`[conn] ${now()} ${peer} close ${Date.now() - t0}ms rx=${sock.bytesRead} tx=${sock.bytesWritten}${hadError ? ' error' : ''}`)
            })
          })
          httpServer.on('secureConnection', (tls) => {
            if (isLoopback(tls)) return
            console.log(`[conn] ${now()} ${peerOf(tls)} tls-ok ${tls.getProtocol?.() || '?'} alpn=${tls.alpnProtocol || '-'} resumed=${tls.isSessionReused?.() ? 1 : 0} sni=${tls.servername || '-'}`)
          })
          httpServer.on('tlsClientError', (err, tls) => {
            if (tls && isLoopback(tls)) return
            console.log(`[conn] ${now()} ${tls ? peerOf(tls) : '?'} tls-error sni=${tls?.servername || '-'} ${err.code || ''} ${String(err.message || err).slice(0, 160)}`)
          })
        }
        // After Vite's SPA fallback has rewritten "/" and app routes to
        // /index.html, before its own index.html middleware.
        return () => {
          server.middlewares.use((req, res, next) => {
            if (isIndexRequest(req)) return serveIndex(req, res)
            next()
          })
        }
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
      //
      // injectRegister: false — the app registers the SW itself
      // (src/lib/swUpdate.js, called from main.jsx) instead of through
      // virtual:pwa-register, whose register code reloads open pages the
      // moment an updated SW activates (vite-plugin-pwa
      // client/build/register.js), even mid-password on /login. Ours defers
      // that reload while the user is on /login or typing. registerType now
      // only feeds that unused virtual module (and, with injectRegister
      // false, no longer implies skipWaiting / clientsClaim), so both stay
      // explicit below.
      //
      // No Workbox route matches navigations or /api requests (see
      // sw-runtime-caching.js for why: a NetworkOnly route turned every
      // transient fetch failure inside the SW into an unrecoverable
      // "FetchEvent.respondWith received an error" page on iOS).
      // Navigations are passed through by public/sw-navigation.js, which
      // answers with its offline page when the network fetch fails.
      registerType: 'autoUpdate',
      injectRegister: false,
      devOptions: { enabled: true },
      workbox: {
        skipWaiting: true,
        clientsClaim: true,
        // No 'html' here — index.html is NOT precached. Navigations always
        // go to the network (sw-navigation.js), never to a stale cached
        // shell; on failure the SW serves its inline offline page.
        globPatterns: ['**/*.{js,css,ico,png,svg,woff2}'],
        // Imported into the generated SW, in this order, before Workbox
        // registers its own fetch listener:
        //  - sw-navigation.js: top-level navigations — pass through to the
        //    network, and on any failure answer with an inline
        //    auto-retrying "Reconnecting…" page (never rejects respondWith)
        //  - push-handler.js: Web Push notifications
        importScripts: ['/sw-navigation.js', '/push-handler.js'],
        // Belt-and-suspenders: explicitly tell Workbox NOT to register a
        // default NavigationRoute for SPA fallback. Without this,
        // vite-plugin-pwa would auto-register one that serves precached
        // index.html as the SPA shell (and could fail the navigation).
        navigateFallback: null,
        runtimeCaching,
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
      // bypass on EVERY entry: /api/hooks/* (agent hook channel, loopback
      // only) → 404. Keys match the raw URL by prefix but the proxy resolves
      // dot segments, so /ws/../api/hooks/x would reach the hooks otherwise.
      // It also overwrites X-Forwarded-For/-Proto/-Host with the real peer
      // (the backend's login limiter keys on it). Targets are the IPv4
      // literal: uvicorn trusts forwarded headers only from 127.0.0.1, and a
      // "localhost" that resolved to ::1 would silently drop them.
      '/api': { target: 'http://127.0.0.1:8080', ws: true, bypass: proxyBypass },
      '/ws': { target: 'ws://127.0.0.1:8080', ws: true, bypass: proxyBypass },
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
      // bypass on EVERY entry: /api/hooks/* (agent hook channel, loopback
      // only) → 404. Keys match the raw URL by prefix but the proxy resolves
      // dot segments, so /ws/../api/hooks/x would reach the hooks otherwise.
      // It also overwrites X-Forwarded-For/-Proto/-Host with the real peer
      // (the backend's login limiter keys on it). Targets are the IPv4
      // literal: uvicorn trusts forwarded headers only from 127.0.0.1, and a
      // "localhost" that resolved to ::1 would silently drop them.
      '/api': { target: 'http://127.0.0.1:8080', ws: true, bypass: proxyBypass },
      '/ws': { target: 'ws://127.0.0.1:8080', ws: true, bypass: proxyBypass },
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
