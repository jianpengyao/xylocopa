// Workbox runtimeCaching routes for the generated service worker
// (consumed by vite.config.js; unit-tested in src/lib/swRuntimeCaching.test.js).
//
// INVARIANT — no Workbox route may match a navigation request or an /api/
// request.
//
// Workbox only calls event.respondWith() when a registered route matches;
// an unmatched request is handled by the browser's own network stack
// "as if there were no service worker present". /api/ requests stay
// native. Top-level navigations are handled by public/sw-navigation.js
// (imported ahead of Workbox), whose handler passes the network response
// through and, on any failure, answers with an inline auto-retrying
// "Reconnecting…" page — it can never reject respondWith.
//
// The previous config routed navigations, /api/* and a catch-all through
// NetworkOnly. NetworkOnly is still a handler: when fetch() throws inside
// the SW (Tailscale flap, backgrounded PWA, TLS), Workbox rejects
// respondWith with WorkboxError('no-response') and iOS renders
//   "Safari can't open the page. The error was: FetchEvent.respondWith
//    received an error: no-response ..."
// in the standalone PWA — a page with no retry affordance that every
// relaunch re-enters through the same SW, so the only way out was clearing
// website data. Do not add navigation / API / catch-all routes back.
//
// Only cacheable, non-critical image assets are routed here. A failure in
// one of these routes breaks a single <img>, never the page or an API call.
export const runtimeCaching = [
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
]
