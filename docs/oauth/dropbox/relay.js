/**
 * Xylocopa Dropbox OAuth relay — static return page.
 *
 * Two hops:
 *
 *   Hop 1 ("start"):
 *     The user's xylocopa instance links here with a fragment:
 *       https://jianpengyao.github.io/xylocopa/oauth/dropbox/#return=<origin>&authorize=<url>
 *     We store the origin in localStorage (so it survives the redirect), then
 *     navigate the browser to the Dropbox authorize URL.
 *
 *   Hop 2 ("return"):
 *     Dropbox redirects back here with ?code=...&state=... (or ?error=...).
 *     We read the stored origin, build the callback URL as
 *       <stored_origin>/api/dropbox/callback?code=...&state=...
 *     and navigate there.  The origin is NEVER taken from the callback query
 *     string — only from localStorage — so this page is not an open redirector.
 *
 * If localStorage is unavailable or empty when a code/error arrives, the page
 * degrades to a small form asking the user for their instance address.
 */

export const STORAGE_KEY = "xylocopa.dropbox.return";

/**
 * Parse a hash or search string into a plain object.
 * Accepts "#a=1&b=2", "?a=1&b=2", or "a=1&b=2".
 */
export function parseParams(hashOrSearch) {
  if (!hashOrSearch) return {};
  const str = hashOrSearch.replace(/^[#?]/, "");
  if (!str) return {};
  const out = {};
  for (const part of str.split("&")) {
    if (!part) continue;
    const eq = part.indexOf("=");
    const key = eq < 0 ? part : part.slice(0, eq);
    const value = eq < 0 ? "" : part.slice(eq + 1);
    // A malformed escape (e.g. "%E0%A4%A") must not take the page down.
    let k, v;
    try { k = decodeURIComponent(key); v = decodeURIComponent(value); } catch { continue; }
    out[k] = v;
  }
  return out;
}

/**
 * Validate that an origin is allowed as a return target.
 * - https://host[:port] — always allowed
 * - http://host[:port] — only for localhost, 127.0.0.1, [::1], 10.x, 172.16-31.x,
 *   192.168.x, *.local
 */
export function isAllowedReturn(origin) {
  if (typeof origin !== "string" || !origin) return false;
  let url;
  try {
    url = new URL(origin);
  } catch {
    return false;
  }
  // Must be exactly an origin (no path beyond "/", no search, no hash)
  if (url.pathname !== "/" || url.search || url.hash) return false;

  if (url.protocol === "https:") return true;
  if (url.protocol !== "http:") return false;

  // http: only for local addresses
  const host = url.hostname.toLowerCase();
  if (host === "localhost" || host === "127.0.0.1" || host === "[::1]" || host === "::1") return true;
  if (host.endsWith(".local")) return true;

  // 10.0.0.0/8
  if (/^10\./.test(host)) return true;
  // 172.16.0.0/12 (172.16.x – 172.31.x)
  const m172 = host.match(/^172\.(\d+)\./);
  if (m172) {
    const second = parseInt(m172[1], 10);
    if (second >= 16 && second <= 31) return true;
  }
  // 192.168.0.0/16
  if (/^192\.168\./.test(host)) return true;

  return false;
}

/**
 * Validate that a URL is a Dropbox authorization URL.
 */
export function isDropboxAuthorizeUrl(url) {
  if (typeof url !== "string") return false;
  return url.startsWith("https://www.dropbox.com/oauth2/authorize?");
}

/**
 * Decide what action to take based on the current page state.
 *
 * @param {{ hash: string, search: string, storage: Storage|null }} ctx
 * @returns {{ action: "start"|"return"|"ask"|"idle", ... }}
 */
export function decide({ hash, search, storage }) {
  const hashParams = parseParams(hash);
  const searchParams = parseParams(search);

  // Hop 1: fragment has return + authorize
  if (hashParams.return && hashParams.authorize) {
    const origin = hashParams.return;
    const authorize = hashParams.authorize;
    if (isAllowedReturn(origin) && isDropboxAuthorizeUrl(authorize)) {
      // Store the origin for hop 2
      if (storage) {
        try { storage.setItem(STORAGE_KEY, origin); } catch { /* blocked */ }
      }
      return { action: "start", authorize, origin };
    }
  }

  // Hop 2: search has code or error (Dropbox callback)
  if (searchParams.code || searchParams.error) {
    let storedOrigin = null;
    if (storage) {
      try { storedOrigin = storage.getItem(STORAGE_KEY); } catch { /* blocked */ }
    }

    if (storedOrigin && isAllowedReturn(storedOrigin)) {
      // Clear storage
      if (storage) {
        try { storage.removeItem(STORAGE_KEY); } catch { /* ignore */ }
      }
      const url = storedOrigin + "/api/dropbox/callback" + search;
      return { action: "return", url };
    }

    // Storage empty or blocked — ask the user
    return { action: "ask", query: search };
  }

  // Nothing to do
  return { action: "idle" };
}

/**
 * Run the relay logic using the real window object.
 * Navigates via location.replace (no history entry).
 */
export function run(win) {
  if (typeof win === "undefined") win = window;

  let storage = null;
  try { storage = win.localStorage; } catch { /* blocked */ }

  const result = decide({
    hash: win.location.hash,
    search: win.location.search,
    storage,
  });

  if (result.action === "start") {
    win.location.replace(result.authorize);
  } else if (result.action === "return") {
    win.location.replace(result.url);
  }
  // "ask" and "idle" are handled by the HTML (the module sets data attributes)

  return result;
}
