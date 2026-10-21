/**
 * TerminalOverlay — full-screen interactive terminal attached to an agent's
 * tmux session (Termius-style). Bridges xterm.js to /ws/terminal/{agentId};
 * closing the overlay only detaches the web tmux client — the session and
 * the Claude process inside it keep running.
 *
 * Protocol (mirrors orchestrator/routers/terminal.py):
 *   send: {type:"input", data} | {type:"resize", cols, rows}
 *   recv: binary PTY bytes | {type:"exit"} | {type:"error", message}
 *
 * Mobile: a key bar (Esc/Tab/sticky-Ctrl/arrows) covers what soft keyboards
 * lack; visualViewport resize shrinks the overlay above the keyboard.
 *
 * Auth: with auth on, each connect trades the session for a one-time ticket
 * (POST /api/auth/ws-ticket) and opens ?ticket=…. A 4001 close with
 * expired / revoked / idle ends the session: no retries, api.js's expiry
 * flow takes the app to /login. 4001 missing / invalid retries like a drop
 * (at most 3 times until a server frame arrives).
 */
import { useCallback, useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { ArrowLeft, RotateCw } from "lucide-react";
import { Terminal } from "@xterm/xterm";
import { FitAddon } from "@xterm/addon-fit";
import "@xterm/xterm/css/xterm.css";
import { AuthError, expireSession, fetchWsTicket, getAuthToken, withTimeout } from "../lib/api";
import { WS_AUTH_CLOSE_CODE, WS_HANDSHAKE_TIMEOUT_MS, wsSessionEndReason } from "../lib/reconnect";
import { getTerminalTheme, THEME_EVENT } from "../lib/themes";
import { useAuth } from "../contexts/AuthContext";

const SESSION_ENDED_MSG = "Session locked — sign in again";

const TOUCH_DEVICE =
  typeof window !== "undefined" &&
  ("ontouchstart" in window || navigator.maxTouchPoints > 0);

const STATUS_META = {
  connecting: { color: "#d29922", label: "Connecting…" },
  connected: { color: "#3fb950", label: "Connected" },
  disconnected: { color: "#f85149", label: "Disconnected" },
  exited: { color: "#8b949e", label: "Detached" },
  error: { color: "#f85149", label: "Error" },
};

// Escape sequences for the mobile key bar
const KEYS = [
  { label: "Esc", seq: "\x1b" },
  { label: "Tab", seq: "\t" },
  { label: "Ctrl", ctrl: true },
  { label: "←", seq: "\x1b[D" },
  { label: "↓", seq: "\x1b[B" },
  { label: "↑", seq: "\x1b[A" },
  { label: "→", seq: "\x1b[C" },
];

export default function TerminalOverlay({ agentId, agentName, onClose }) {
  const { authReady, authDisabled, refresh: refreshAuth } = useAuth();
  const useTicket = authReady && !authDisabled;
  const refreshAuthRef = useRef(refreshAuth);
  refreshAuthRef.current = refreshAuth;
  const rootRef = useRef(null);
  const mountRef = useRef(null);
  const termRef = useRef(null);
  const fitRef = useRef(null);
  const wsRef = useRef(null);
  const unmountedRef = useRef(false);
  const ctrlArmedRef = useRef(false);
  // Auto-reconnect bookkeeping: tmux keeps the session alive server-side,
  // so a dropped socket (phone lock, backgrounded PWA, network blip) can
  // silently re-attach. serverEnded = clean exit/error → manual only.
  const retriesRef = useRef(0);
  const reconnectTimerRef = useRef(null);
  const pendingVisRef = useRef(false);
  const serverEndedRef = useRef(false);
  // Ticket fetch bookkeeping: the in-flight flag keeps the visibility
  // handler from starting a second connect; the generation drops the result
  // of a fetch that a newer connect (or unmount) superseded.
  const ticketInFlightRef = useRef(false);
  const connectGenRef = useRef(0);
  const [status, setStatus] = useState("connecting");
  const [errMsg, setErrMsg] = useState("");
  const [ctrlArmed, setCtrlArmed] = useState(false);
  // xterm theme follows the app palette; hot-swapped if it changes while open.
  const [termTheme, setTermTheme] = useState(getTerminalTheme);

  useEffect(() => {
    const onThemeChange = () => {
      const next = getTerminalTheme();
      setTermTheme(next);
      if (termRef.current) termRef.current.options.theme = next;
    };
    window.addEventListener(THEME_EVENT, onThemeChange);
    return () => window.removeEventListener(THEME_EVENT, onThemeChange);
  }, []);

  const armCtrl = (on) => {
    ctrlArmedRef.current = on;
    setCtrlArmed(on);
  };

  // Fit terminal to container and propagate the new grid to the PTY
  const doFit = useCallback(() => {
    try {
      fitRef.current?.fit();
    } catch {
      return;
    }
    const t = termRef.current;
    const ws = wsRef.current;
    if (t && ws?.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({ type: "resize", cols: t.cols, rows: t.rows }));
    }
  }, []);

  const sendInput = useCallback((data) => {
    const ws = wsRef.current;
    if (ws?.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({ type: "input", data }));
    }
  }, []);

  // A dropped socket (or a failed ticket fetch): re-attach silently, up to
  // 3 times; while backgrounded, the moment the page is visible again.
  const retryAfterDrop = useCallback(() => {
    if (unmountedRef.current || serverEndedRef.current) return;
    if (document.hidden) {
      // Backgrounded — reconnect the moment we're visible again
      pendingVisRef.current = true;
      setStatus("disconnected");
      return;
    }
    if (retriesRef.current < 3) {
      retriesRef.current += 1;
      setStatus("connecting");
      clearTimeout(reconnectTimerRef.current);
      reconnectTimerRef.current = setTimeout(() => {
        if (!unmountedRef.current) reconnectRef.current?.();
      }, 800 * retriesRef.current);
    } else {
      setStatus("disconnected");
    }
  }, []);

  // The server ended the session: no retries. api.js's expiry flow
  // (auth-expired) takes the app to /login.
  const endSession = useCallback(() => {
    serverEndedRef.current = true;
    setStatus("error");
    setErrMsg(SESSION_ENDED_MSG);
  }, []);

  // `sessionToken`: the stored session the ticket was minted for, so a
  // session-ending close never expires a newer login from another tab.
  const openSocket = useCallback((ticket, sessionToken) => {
    const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
    const url =
      `${proto}//${window.location.host}/ws/terminal/${encodeURIComponent(agentId)}` +
      (ticket ? `?ticket=${encodeURIComponent(ticket)}` : "");
    const ws = new WebSocket(url);
    ws.binaryType = "arraybuffer";
    wsRef.current = ws;

    ws.onopen = () => {
      pendingVisRef.current = false;
      setStatus("connected");
      doFit();
      termRef.current?.focus();
    };
    ws.onmessage = (ev) => {
      // Only a frame proves the attach: the server accepts before its auth
      // check (4001) and before a store error (1011), so resetting the
      // retries on open would loop forever on a persistent failure.
      retriesRef.current = 0;
      if (typeof ev.data === "string") {
        let m;
        try {
          m = JSON.parse(ev.data);
        } catch {
          return;
        }
        if (m.type === "exit") {
          serverEndedRef.current = true;
          setStatus("exited");
        } else if (m.type === "error") {
          serverEndedRef.current = true;
          setStatus("error");
          setErrMsg(m.message || "Connection error");
        }
        return;
      }
      termRef.current?.write(new Uint8Array(ev.data));
    };
    ws.onclose = (ev) => {
      // A superseded socket closing late must not start another retry.
      if (unmountedRef.current || serverEndedRef.current || wsRef.current !== ws) return;
      const endReason = wsSessionEndReason(ev);
      if (endReason) {
        const current = getAuthToken();
        if (!(sessionToken && current && current !== sessionToken)) {
          endSession();
          expireSession(endReason, { requestToken: sessionToken, source: "ws-terminal" }).catch(() => {});
          return;
        }
        // That session was already replaced by a newer login: retry with it.
      }
      // Opened without a ticket, yet the server wants one (DISABLE_AUTH was
      // flipped off under this tab): re-run the auth check; once AuthGuard
      // knows auth is on, this overlay reconnects with a ticket.
      if (!ticket && ev?.code === WS_AUTH_CLOSE_CODE) {
        Promise.resolve().then(() => refreshAuthRef.current?.()).catch(() => {});
      }
      retryAfterDrop();
    };
  }, [agentId, doFit, endSession, retryAfterDrop]);

  const connect = useCallback(() => {
    clearTimeout(reconnectTimerRef.current); // this connect supersedes a queued retry
    setStatus("connecting");
    setErrMsg("");
    serverEndedRef.current = false;
    // The old socket (if any) was closed by reconnect(); its late onclose
    // is ignored from here on.
    wsRef.current = null;
    const sessionToken = getAuthToken();
    const gen = ++connectGenRef.current;
    if (!useTicket) {
      ticketInFlightRef.current = false;
      openSocket(null, sessionToken);
      return;
    }
    ticketInFlightRef.current = true;
    withTimeout((signal) => fetchWsTicket({ signal }), WS_HANDSHAKE_TIMEOUT_MS).then(
      (r) => {
        if (gen !== connectGenRef.current || unmountedRef.current) return;
        ticketInFlightRef.current = false;
        openSocket(r?.ticket || null, sessionToken);
      },
      (err) => {
        if (gen !== connectGenRef.current || unmountedRef.current) return;
        ticketInFlightRef.current = false;
        if (err instanceof AuthError) {
          const current = getAuthToken();
          // api.js already ran the expiry flow — unless a newer login won.
          if (current && current !== sessionToken) retryAfterDrop();
          else endSession();
          return;
        }
        retryAfterDrop();
      },
    );
  }, [useTicket, openSocket, endSession, retryAfterDrop]);

  const reconnect = useCallback(() => {
    try {
      wsRef.current?.close();
    } catch {}
    termRef.current?.reset();
    connect();
  }, [connect]);
  const reconnectRef = useRef(null);
  reconnectRef.current = reconnect;

  // Mount: create terminal, wire input, connect, observe resizes
  useEffect(() => {
    unmountedRef.current = false;
    const term = new Terminal({
      cursorBlink: true,
      fontSize: window.innerWidth < 640 ? 13 : 14,
      fontFamily:
        "ui-monospace, SFMono-Regular, Menlo, Consolas, 'Liberation Mono', monospace",
      theme: getTerminalTheme(),
      scrollback: 2000,
    });
    const fit = new FitAddon();
    term.loadAddon(fit);
    termRef.current = term;
    fitRef.current = fit;
    term.open(mountRef.current);
    try {
      fit.fit();
    } catch {}

    term.onData((d) => {
      let out = d;
      if (ctrlArmedRef.current) {
        if (d.length === 1) {
          const c = d.toUpperCase().charCodeAt(0);
          if (c >= 64 && c <= 95) out = String.fromCharCode(c & 0x1f);
        }
        ctrlArmedRef.current = false;
        setCtrlArmed(false);
      }
      sendInput(out);
    });

    connect();

    // Container resize (rotation, split view) → refit
    let raf = 0;
    const ro = new ResizeObserver(() => {
      cancelAnimationFrame(raf);
      raf = requestAnimationFrame(doFit);
    });
    ro.observe(mountRef.current);

    // Soft keyboard: pin the pane to the *visual* viewport — shrink to its
    // height AND translate by its offsetTop. iOS scrolls the page to keep
    // the focused input visible, so height alone leaves a strip of the
    // underlying app peeking out right above the keyboard.
    const vv = window.visualViewport;
    const onVV = () => {
      if (rootRef.current && vv) {
        const h = Math.round(vv.height);
        const shrunk = h < window.innerHeight - 30;
        rootRef.current.style.height = shrunk ? `${h}px` : "";
        rootRef.current.style.transform =
          shrunk && vv.offsetTop > 1
            ? `translateY(${Math.round(vv.offsetTop)}px)`
            : "";
      }
      cancelAnimationFrame(raf);
      raf = requestAnimationFrame(doFit);
    };
    vv?.addEventListener("resize", onVV);
    vv?.addEventListener("scroll", onVV);

    // Keepalive: mobile networks and proxies drop idle sockets; the
    // backend answers {"type":"pong"} and the traffic keeps NAT mappings
    // warm while the terminal is just being read.
    const keepalive = setInterval(() => {
      const w = wsRef.current;
      if (w?.readyState === WebSocket.OPEN) {
        w.send(JSON.stringify({ type: "ping" }));
      }
    }, 25000);

    // Coming back from background: iOS suspends the socket; re-attach
    // immediately instead of showing a stale screen + manual button.
    const onVis = () => {
      if (document.hidden || unmountedRef.current || serverEndedRef.current) return;
      if (ticketInFlightRef.current) return; // its socket follows
      const w = wsRef.current;
      const dead =
        pendingVisRef.current ||
        !w ||
        w.readyState === WebSocket.CLOSED ||
        w.readyState === WebSocket.CLOSING;
      if (dead) {
        pendingVisRef.current = false;
        retriesRef.current = 0;
        reconnectRef.current?.();
      }
    };
    document.addEventListener("visibilitychange", onVis);

    return () => {
      unmountedRef.current = true;
      document.removeEventListener("visibilitychange", onVis);
      clearInterval(keepalive);
      clearTimeout(reconnectTimerRef.current);
      vv?.removeEventListener("resize", onVV);
      vv?.removeEventListener("scroll", onVV);
      ro.disconnect();
      cancelAnimationFrame(raf);
      try {
        wsRef.current?.close();
      } catch {}
      term.dispose();
      termRef.current = null;
    };
  }, [connect, doFit, sendInput]);

  const handleKeyButton = (key) => {
    if (key.ctrl) {
      armCtrl(!ctrlArmedRef.current);
      return;
    }
    if (ctrlArmedRef.current && key.seq.length === 1) {
      // e.g. Ctrl+Tab is meaningless here, but Ctrl+letter via bar stays consistent
      const c = key.seq.toUpperCase().charCodeAt(0);
      sendInput(c >= 64 && c <= 95 ? String.fromCharCode(c & 0x1f) : key.seq);
      armCtrl(false);
      return;
    }
    sendInput(key.seq);
  };

  const meta = STATUS_META[status] || STATUS_META.connecting;
  const showBanner = status === "disconnected" || status === "exited" || status === "error";

  return createPortal(
    // Outer backdrop never resizes: even while the inner pane is being
    // shrunk/translated around the soft keyboard, the app underneath can
    // never peek through.
    <div
      className="fixed inset-0 z-[120] overflow-hidden"
      style={{ background: termTheme.background }}
    >
      {/* iOS zooms into focused inputs with font-size < 16px; xterm's hidden
          helper textarea triggers that. Scoped override while overlay is open. */}
      <style>{`.xterm-helper-textarea { font-size: 16px !important; }`}</style>
      <div
        ref={rootRef}
        className="absolute inset-x-0 top-0 h-full flex flex-col safe-area-pt"
      >

      {/* Header */}
      <div className="shrink-0 h-11 flex items-center gap-2 px-2 border-b border-divider bg-surface">
        <button
          type="button"
          onClick={onClose}
          title="Close (detach — session keeps running)"
          className="w-8 h-8 flex items-center justify-center rounded-lg text-dim hover:text-heading hover:bg-hover transition-colors"
        >
          <ArrowLeft className="w-4.5 h-4.5" strokeWidth={2} />
        </button>
        <span
          className="w-2 h-2 rounded-full shrink-0"
          style={{
            background: meta.color,
            animation: status === "connecting" ? "pulse 1.2s ease-in-out infinite" : "none",
          }}
        />
        <div className="min-w-0 flex-1">
          <div className="text-[13px] font-medium text-heading truncate leading-tight">
            {agentName || "Terminal"}
          </div>
          <div className="text-[10px] text-dim leading-tight">
            tmux · {meta.label}
          </div>
        </div>
        <div className="text-[10px] text-faint pr-1 text-right shrink-0">
          close = detach only
        </div>
      </div>

      {/* Terminal */}
      <div className="flex-1 min-h-0 relative pl-2 pt-1" style={{ background: termTheme.background }}>
        <div ref={mountRef} className="absolute inset-0 pl-2 pr-1 pt-1" />
        {showBanner && (
          <div className="absolute inset-0 z-10 flex items-center justify-center bg-black/60">
            <div className="flex flex-col items-center gap-3 px-6 py-5 rounded-2xl border border-edge bg-surface">
              <div className="text-sm text-body">
                {status === "error"
                  ? errMsg || "Connection error"
                  : status === "exited"
                    ? "tmux client detached"
                    : "Connection lost"}
              </div>
              <button
                type="button"
                onClick={reconnect}
                className="flex items-center gap-1.5 px-4 py-1.5 rounded-lg bg-accent-90 text-accent-ink text-sm transition-opacity hover:opacity-90"
              >
                <RotateCw className="w-3.5 h-3.5" strokeWidth={2} />
                Reconnect
              </button>
            </div>
          </div>
        )}
      </div>

      {/* Mobile key bar */}
      {TOUCH_DEVICE && (
        <div
          className="shrink-0 flex items-stretch gap-1 px-1.5 pt-1 border-t border-divider bg-surface"
          style={{ paddingBottom: "max(0.25rem, env(safe-area-inset-bottom))" }}
        >
          {KEYS.map((k) => (
            <button
              key={k.label}
              type="button"
              // pointerdown + preventDefault keeps focus (and the soft
              // keyboard) on the terminal while tapping bar keys
              onPointerDown={(e) => {
                e.preventDefault();
                handleKeyButton(k);
              }}
              className={`flex-1 h-9 rounded-md text-[13px] font-medium transition-colors ${
                k.ctrl && ctrlArmed
                  ? "bg-accent text-accent-ink"
                  : "bg-input text-body active:bg-hover"
              }`}
            >
              {k.label}
            </button>
          ))}
        </div>
      )}
      </div>
    </div>,
    document.body
  );
}
