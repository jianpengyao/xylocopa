/* Xylocopa Service Worker — navigation handling.
 *
 * Imported into the generated sw.js (vite.config.js `importScripts`), so this
 * fetch listener is registered before Workbox's and sees every top-level
 * navigation first. No Workbox route handles navigations (see
 * sw-runtime-caching.js).
 *
 * CONTRACT: this handler can never fail a page load. It passes the network
 * response through, and on ANY failure (offline, Tailscale flap, TLS, hang
 * beyond NETWORK_TIMEOUT_MS) it responds with the inline "Reconnecting…"
 * page below instead of rejecting respondWith. A rejected respondWith is
 * what produced the unrecoverable "Safari can't open the page. The error
 * was: FetchEvent.respondWith received an error: no-response" screen on
 * iOS, where a standalone PWA has no reload button and every relaunch
 * re-enters the same handler.
 *
 * The page is inline (not a cached file) so it is available even right
 * after the recovery paths in index.html wipe Cache Storage.
 *
 * The offline page:
 *   - probes GET /api/health natively (no Workbox route matches /api, so
 *     the probe bypasses this worker) with 1s→5s backoff, instantly on
 *     foreground/online, and reloads into the app when the server answers;
 *   - detects the "service-worker fetch is broken while the page's own
 *     fetch works" state: landing here again right after a successful
 *     probe counts as a bounce; two bounces in a row unregister the worker
 *     and clear caches, then reload natively — the manual "clear website
 *     data" fix, automated. index.html clears the counters on a good load.
 */
(function () {
  "use strict";

  var NETWORK_TIMEOUT_MS = 15000;

  var OFFLINE_HTML = [
    '<!doctype html>',
    '<html lang="en"><head>',
    '<meta charset="utf-8">',
    '<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">',
    '<meta name="apple-mobile-web-app-capable" content="yes">',
    '<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">',
    '<meta name="theme-color" content="#0a0a0a">',
    '<title>Xylocopa</title>',
    '<style>',
    ':root{color-scheme:light dark}',
    'html,body{height:100%;margin:0}',
    'body{display:flex;align-items:center;justify-content:center;font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;background:#fff;color:#111;padding:env(safe-area-inset-top) env(safe-area-inset-right) env(safe-area-inset-bottom) env(safe-area-inset-left)}',
    '@media (prefers-color-scheme:dark){body{background:#0a0a0a;color:#e5e7eb}}',
    '.box{text-align:center;padding:24px;max-width:320px}',
    '.spin{width:28px;height:28px;margin:0 auto 16px;border:3px solid rgba(6,182,212,.25);border-top-color:#06b6d4;border-radius:50%;animation:s .9s linear infinite}',
    '@keyframes s{to{transform:rotate(360deg)}}',
    'h1{font-size:17px;font-weight:600;margin:0 0 6px}',
    'p{margin:0 0 18px;opacity:.7}',
    'button{font:inherit;padding:10px 18px;border-radius:10px;border:0;background:#06b6d4;color:#fff}',
    '.reset{display:block;margin:18px auto 0;font-size:12px;opacity:.55;background:none;color:inherit;text-decoration:underline;padding:0}',
    '</style></head>',
    '<body><div class="box">',
    '<div class="spin"></div>',
    '<h1>Reconnecting…</h1>',
    '<p id="msg">Waiting for the server. This page retries on its own.</p>',
    '<button id="retry" type="button">Retry now</button>',
    '<button id="reset" class="reset" type="button">Reset app data</button>',
    '</div>',
    '<script>',
    '(function(){',
    'var FLAG="xy-offline-probe-ok",BOUNCES="xy-offline-bounces",MAX_BOUNCES=2;',
    'var ss={get:function(k){try{return sessionStorage.getItem(k)}catch(_){return null}},set:function(k,v){try{sessionStorage.setItem(k,v)}catch(_){}},del:function(k){try{sessionStorage.removeItem(k)}catch(_){}}};',
    'var msg=document.getElementById("msg");',
    'function reset(){',
    ' msg.textContent="Resetting the app cache\\u2026";',
    ' var p=("serviceWorker" in navigator)?navigator.serviceWorker.getRegistrations().then(function(rs){return Promise.all(rs.map(function(r){return r.unregister()}))}):Promise.resolve();',
    ' p.catch(function(){}).then(function(){return ("caches" in window)?caches.keys().then(function(ns){return Promise.all(ns.map(function(n){return caches.delete(n)}))}):null}).catch(function(){}).then(function(){ss.del(FLAG);ss.del(BOUNCES);location.reload()});',
    '}',
    '/* Landed here right after a probe said the server is up: the load failed inside the service worker while the page\'s own fetch worked. Twice in a row means the worker itself is stuck. */',
    'if(ss.get(FLAG)==="1"){',
    ' ss.del(FLAG);',
    ' var n=Number(ss.get(BOUNCES)||0)+1;ss.set(BOUNCES,String(n));',
    ' if(n>=MAX_BOUNCES&&navigator.onLine!==false){reset();return}',
    '}else{ss.del(BOUNCES)}',
    'var delay=1000,timer=null,busy=false,started=Date.now();',
    'function schedule(ms){clearTimeout(timer);timer=setTimeout(probe,ms)}',
    'function probe(){',
    ' if(busy)return;busy=true;timer=null;',
    ' var ctl=("AbortController" in window)?new AbortController():null;',
    ' var kill=ctl?setTimeout(function(){ctl.abort()},8000):null;',
    ' fetch("/api/health",{cache:"no-store",credentials:"same-origin",signal:ctl&&ctl.signal})',
    '  .then(function(r){if(!r.ok)throw new Error(String(r.status));ss.set(FLAG,"1");location.reload()})',
    '  .catch(function(){busy=false;if(kill)clearTimeout(kill);var s=Math.round((Date.now()-started)/1000);msg.textContent="Still waiting for the server ("+s+"s). This page retries on its own.";schedule(delay);delay=Math.min(delay*1.5,5000)});',
    '}',
    '/* iOS 18: a fetch issued synchronously on foreground can fail with "Load failed"; defer the resume probe slightly. */',
    'function soon(){delay=1000;clearTimeout(timer);timer=setTimeout(probe,500)}',
    'document.getElementById("retry").addEventListener("click",function(){delay=1000;clearTimeout(timer);probe()});',
    'document.getElementById("reset").addEventListener("click",reset);',
    'window.addEventListener("online",soon);',
    'document.addEventListener("visibilitychange",function(){if(document.visibilityState==="visible")soon()});',
    'schedule(delay);',
    '})();',
    '</script></body></html>',
  ].join("\n");

  function offlineResponse() {
    return new Response(OFFLINE_HTML, {
      status: 503,
      headers: {
        "Content-Type": "text/html; charset=utf-8",
        "Cache-Control": "no-store",
      },
    });
  }

  function withTimeout(promise, ms) {
    return new Promise(function (resolve, reject) {
      var timer = setTimeout(function () {
        reject(new Error("navigation fetch timed out after " + ms + "ms"));
      }, ms);
      promise.then(
        function (value) { clearTimeout(timer); resolve(value); },
        function (err) { clearTimeout(timer); reject(err); }
      );
    });
  }

  function handleNavigation(request) {
    var attempt;
    try {
      attempt = withTimeout(fetch(request), NETWORK_TIMEOUT_MS);
    } catch {
      return Promise.resolve(offlineResponse());
    }
    return attempt.then(
      function (response) { return response || offlineResponse(); },
      function () { return offlineResponse(); }
    );
  }

  self.addEventListener("fetch", function (event) {
    var request = event.request;
    if (!request || request.mode !== "navigate" || request.method !== "GET") return;
    // Only top-level documents. Frames (e.g. file previews) keep the
    // browser's native handling; an offline page inside an iframe would
    // be confusing and the parent page already handles its own errors.
    if (request.destination && request.destination !== "document") return;
    event.respondWith(
      handleNavigation(request).catch(function () { return offlineResponse(); })
    );
  });
})();
