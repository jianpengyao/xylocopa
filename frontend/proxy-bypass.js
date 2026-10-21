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
