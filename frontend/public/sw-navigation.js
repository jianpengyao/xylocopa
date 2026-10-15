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
 *   - probes GET /api/health with 1s→5s backoff (the page is controlled by
 *     this worker, so the probe is dispatched here too; no route answers
 *     /api, so it goes straight to the network) and reloads into the app
 *     as soon as the server answers with any HTTP status;
 *   - "Retry now", returning to the foreground and the "online" event abort
 *     a probe still in flight and start a fresh one — a probe that began
 *     before the user toggled their VPN hangs on a dead connection until
 *     its 8s abort, and used to swallow every tap in the meantime;
 *   - says what failed (timeout vs connection error), and after a couple
 *     of failures tells the user the device cannot reach the server and to
 *     toggle Tailscale/VPN. No page-side action can repair the network, and
 *     every failure seen so far was the device's tunnel, not this app;
 *   - "Reset app data" only resets after a probe proves the server is
 *     reachable. During an outage it would just remove this page (the
 *     next launch gets the browser's dead error page) and the push
 *     subscription, and its reload would hang on the same network;
 *   - detects the "service-worker fetch is broken while the page's own
 *     fetch works" state: landing here again right after a successful
 *     probe counts as a bounce; two bounces in a row unregister the worker
 *     and clear caches, then reload natively — the manual "clear website
 *     data" fix, automated. index.html clears the counters on a good load;
 *   - records its failed probes in localStorage (xy-offline-diag);
 *     index.html reports them to /api/debug/auth-diag once the app loads.
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
    'button:active{opacity:.6}',
    '.hint{margin:0 0 18px;padding:10px 12px;border-radius:10px;background:rgba(6,182,212,.14);opacity:1;font-size:14px;text-align:left}',
    '[hidden]{display:none!important}',
    '.reset{display:block;margin:8px auto 0;font-size:13px;opacity:.6;background:none;color:inherit;text-decoration:underline;padding:12px 16px}',
    '</style></head>',
    '<body><div class="box">',
    '<div class="spin"></div>',
    '<h1>Reconnecting…</h1>',
    '<p id="msg">Waiting for the server. This page retries on its own.</p>',
    '<p id="hint" class="hint" hidden>This device can’t reach the server. If you connect through Tailscale or another VPN, open that app and switch it off and on again, then come back — this page reconnects by itself.</p>',
    '<button id="retry" type="button">Retry now</button>',
    '<button id="reset" class="reset" type="button">Reset app data</button>',
    '</div>',
    '<script>',
    '(function(){',
    'var FLAG="xy-offline-probe-ok",BOUNCES="xy-offline-bounces",DIAG="xy-offline-diag",MAX_BOUNCES=2;',
    'var PROBE_MS=8000,HINT_MS=15000,STUCK_MS=20000;',
    'function store(get){return{get:function(k){try{return get().getItem(k)}catch(_){return null}},set:function(k,v){try{get().setItem(k,v)}catch(_){}},del:function(k){try{get().removeItem(k)}catch(_){}}}}',
    'var ss=store(function(){return sessionStorage}),ls=store(function(){return localStorage});',
    'var msg=document.getElementById("msg"),hint=document.getElementById("hint"),VPN_HINT=hint.textContent;',
    'var OFFLINE_HINT="This device is offline. Turn on Wi-Fi or cellular data \\u2014 this page reconnects by itself.";',
    '/* state: "" = probing, "leaving" = reload in flight, "resetting" = wiping the worker. */',
    'var delay=1000,timer=null,current=null,currentOk=null,seq=0,wd=0,state="",started=Date.now();',
    'function say(t){msg.textContent=t}',
    'function noop(){}',
    '/* Kept across relaunches; index.html reports it once the app loads. */',
    'function note(kind,why){var d=null;try{d=JSON.parse(ls.get(DIAG)||"null")}catch(_){}if(!d||typeof d!=="object")d={first:Date.now(),probes:0,timeouts:0,errors:0};d.probes++;d[kind]++;d.last=Date.now();d.why=why;ls.set(DIAG,JSON.stringify(d))}',
    '/* WebKit keeps this page on screen while a reload is pending, with no request timeout on iOS: if nothing replaced the page, go back to probing. */',
    '/* WebKit fails every fetch from a page whose reload is still pending ("Load failed", never sent), so cancel that navigation first. */',
    'function cancelReload(){try{if(window.stop)window.stop()}catch(_){}}',
    'function watchdog(s){var g=++wd;setTimeout(function(){if(g!==wd||state!==s)return;state="";cancelReload();say("Still loading\\u2026 trying again.");probe(false)},STUCK_MS)}',
    'function reload(){state="leaving";ss.set(FLAG,"1");say("Server reachable \\u2014 loading the app\\u2026");location.reload();watchdog("leaving")}',
    'function wipe(){',
    ' state="resetting";clearTimeout(timer);say("Resetting app data\\u2026");',
    ' var p=("serviceWorker" in navigator)?navigator.serviceWorker.getRegistrations().then(function(rs){return Promise.all(rs.map(function(r){return r.unregister()}))}):Promise.resolve();',
    ' p.catch(noop).then(function(){return ("caches" in window)?caches.keys().then(function(ns){return Promise.all(ns.map(function(n){return caches.delete(n)}))}):null}).catch(noop).then(function(){ss.del(FLAG);ss.del(BOUNCES);location.reload();watchdog("resetting")});',
    '}',
    'function schedule(ms){clearTimeout(timer);timer=setTimeout(function(){probe(false)},ms)}',
    '/* manual (tap, foreground, "online"): supersede a probe still in flight instead of being ignored. onOk runs once the server answers with any HTTP status. */',
    'function probe(manual,onOk){',
    ' if(state)return;',
    ' if(current){if(!manual)return;current.abort()}',
    ' clearTimeout(timer);timer=null;',
    ' var id=++seq,timedOut=false,ctl=("AbortController" in window)?new AbortController():null;',
    ' var kill=setTimeout(function(){timedOut=true;if(ctl)ctl.abort()},PROBE_MS);',
    ' current={abort:function(){clearTimeout(kill);if(ctl)ctl.abort()}};currentOk=onOk;',
    ' if(manual)say(onOk===wipe?"Checking the server before resetting\\u2026":"Checking the server\\u2026");',
    ' fetch("/api/health?src=offline",{cache:"no-store",credentials:"same-origin",signal:ctl&&ctl.signal}).then(function(){',
    '  if(id!==seq)return;clearTimeout(kill);current=null;(onOk||reload)();',
    ' },function(e){',
    '  if(id!==seq)return;clearTimeout(kill);current=null;',
    '  var why=timedOut?"no response in "+(PROBE_MS/1000)+"s":"connection failed"+(e&&e.message?" ("+e.message+")":"");',
    '  note(timedOut?"timeouts":"errors",why);',
    '  var s=Math.round((Date.now()-started)/1000);',
    '  say(onOk===wipe?"The server is unreachable ("+why+"). Resetting would not help \\u2014 it only works once the server answers. Still retrying.":"Can\\u2019t reach the server: "+why+". Offline for "+s+"s, retrying on its own.");',
    '  hint.textContent=navigator.onLine===false?OFFLINE_HINT:VPN_HINT;',
    '  if(s*1000>=HINT_MS)hint.hidden=false;',
    '  schedule(delay);delay=Math.min(delay*1.5,5000);',
    ' });',
    '}',
    '/* Landed here right after a probe said the server is up: the load failed inside the service worker while the page\'s own fetch worked. Twice in a row means the worker itself is stuck. */',
    'if(ss.get(FLAG)==="1"){',
    ' ss.del(FLAG);',
    ' var n=Number(ss.get(BOUNCES)||0)+1;ss.set(BOUNCES,String(n));',
    ' if(n>=MAX_BOUNCES&&navigator.onLine!==false)wipe();',
    '}else{ss.del(BOUNCES)}',
    '/* iOS 18: a fetch issued synchronously on foreground can fail with "Load failed"; defer the resume probe slightly. */',
    'function soon(){if(state)return;var ok=current?currentOk:null;delay=1000;clearTimeout(timer);timer=setTimeout(function(){probe(true,ok)},500)}',
    'document.getElementById("retry").addEventListener("click",function(){if(state==="resetting")return;if(state==="leaving")cancelReload();state="";delay=1000;probe(true)});',
    'document.getElementById("reset").addEventListener("click",function(){if(state)return;probe(true,wipe)});',
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

  function withTimeout(promise, ms, ctl) {
    return new Promise(function (resolve, reject) {
      var timer = setTimeout(function () {
        if (ctl) ctl.abort();
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
      // Abort, not just abandon, a fetch that timed out: a hung request
      // keeps holding a connection, and on HTTP/1.1 WebKit's 6-per-host
      // pool then filled up with them and starved the offline page's
      // probes (repro harness, WebKit). With an init the request is
      // re-created with mode "same-origin" instead of "navigate"; the
      // server does not look at that.
      var ctl = typeof AbortController === "function" ? new AbortController() : null;
      attempt = withTimeout(ctl ? fetch(request, { signal: ctl.signal }) : fetch(request), NETWORK_TIMEOUT_MS, ctl);
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
