import path from 'node:path'

// Vite proxy `bypass` for every proxy entry in vite.config.js
// (unit-tested in src/lib/proxyBypass.test.js).
//
// /api/hooks/* is the agent hook channel. Claude Code's HTTP hooks and
// hooks/session-start.sh call http://localhost:PORT directly over loopback,
// never through this HTTPS proxy, so the proxy refuses the prefix: returning
// false makes Vite answer 404 (HTTP) or end the socket with a 404 (WebSocket
// upgrade) without contacting the backend.
//
// The check runs on the path the backend would route, not on the proxy key:
// Vite picks an entry by a raw startsWith() on req.url, then the proxy
// forwards req.url through WHATWG URL parsing (dot segments, %2e and
// backslashes resolved), and uvicorn percent-decodes it. Mirror both and
// collapse repeated slashes, so /api/%68ooks/..., /api//hooks/... and
// /ws/../api/hooks/... (matched by the '/ws' entry) are refused as well.

const BASE = 'http://proxy.invalid'

export function backendPath(rawUrl) {
  const url = rawUrl || '/'
  let pathname
  try {
    pathname = new URL(url.startsWith('//') ? BASE + url : url, BASE).pathname
  } catch {
    return null
  }
  const decoded = pathname.replace(/%([0-9a-fA-F]{2})/g,
    (_, hex) => String.fromCharCode(parseInt(hex, 16)))
  return path.posix.normalize(decoded.replace(/\/{2,}/g, '/'))
}

export function isHookPath(rawUrl) {
  const p = backendPath(rawUrl)
  if (p === null) return true  // unparseable: refuse rather than guess
  const lower = p.toLowerCase()
  return lower === '/api/hooks' || lower.startsWith('/api/hooks/')
}

export function refuseHookPaths(req) {
  return isHookPath(req.url) ? false : undefined
}

// Real client IP for the backend (login limiter, logs). The backend binds
// 127.0.0.1 and uvicorn trusts X-Forwarded-* only from 127.0.0.1, so every
// request through this proxy is attributed to the address set here — always
// the TCP peer, OVERWRITING whatever the client sent (a client-supplied
// X-Forwarded-For would otherwise let anyone pick the IP that gets locked).
// Runs for HTTP and WebSocket upgrades alike: http-proxy copies req.headers
// into the upstream request.
export function setForwardedHeaders(req) {
  const peer = (req.socket?.remoteAddress || '').replace(/^::ffff:/, '')
  if (peer) req.headers['x-forwarded-for'] = peer
  else delete req.headers['x-forwarded-for']
  req.headers['x-forwarded-proto'] = req.socket?.encrypted ? 'https' : 'http'
  const host = req.headers.host || req.headers[':authority']
  if (host) req.headers['x-forwarded-host'] = host
  else delete req.headers['x-forwarded-host']
  delete req.headers.forwarded
  delete req.headers['x-real-ip']
}

// The `bypass` of every proxy entry (Vite calls it as (req, res, opts)):
// forwarded headers, then the hook refusal.
export function proxyBypass(req) {
  setForwardedHeaders(req)
  return refuseHookPaths(req)
}

// Access-log form of a URL: credentials in query params (media ?token=,
// WebSocket ?ticket=) and path-embedded capabilities are masked.
export function redactUrl(url) {
  return String(url ?? '')
    .replace(/([?&](?:token|ticket)=)[^&\s]+/g, '$1<redacted>')
    .replace(/(\/api\/preview\/[tp]\/)[^/\s?]+/g, '$1<redacted>')
    .replace(/(\/api\/probe-trigger\/)[^/\s?]+/g, '$1<redacted>')
}
