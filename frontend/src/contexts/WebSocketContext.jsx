import { createContext, useContext, useEffect, useRef, useCallback, useState } from "react";
import { AuthError, expireSession, fetchWsTicket, getAuthToken, withTimeout } from "../lib/api";
import { calibrate } from "../lib/serverTime";
import { pickPrimaryAgent } from "../lib/notifications";
import {
  nextBackoff,
  onNetworkResume,
  wsSessionEndReason,
  WS_AUTH_CLOSE_CODE,
  WS_PING_INTERVAL_MS as PING_INTERVAL_MS,
  WS_PONG_TIMEOUT_MS as PONG_TIMEOUT_MS,
  WS_HANDSHAKE_TIMEOUT_MS as HANDSHAKE_TIMEOUT_MS,
  WS_INITIAL_RECONNECT_MS as INITIAL_RECONNECT_MS,
  WS_MAX_RECONNECT_MS as MAX_RECONNECT_MS,
} from "../lib/reconnect";
import { useAuth } from "./AuthContext";

const WebSocketContext = createContext(null);


/**
 * Single shared WebSocket connection for the entire browser tab.
 * Place this provider near the root of the component tree (above all routes).
 *
 * Events are delivered via a subscriber/callback pattern so that rapid
 * successive WebSocket messages are never lost to React 18 batching
 * (which can collapse multiple setState calls into one render).
 *
 * Reconnect policy: never gives up. Backoff 1s × 1.5ⁿ capped at 30s on
 * close; reset and retried immediately when the page is foregrounded or
 * the browser reports the network is back (see lib/reconnect.js).
 *
 * Auth: inside AuthGuard with auth on, every connect first trades the
 * session for a one-time ticket (POST /api/auth/ws-ticket, 30 s, single
 * use) and opens /ws/status?ticket=…. Under DISABLE_AUTH, and outside
 * AuthGuard where there is no session to trade, it connects bare. Only a
 * 4001 close with expired / revoked / idle ends the session (no reconnect);
 * 4001 missing / invalid fetches a fresh ticket with its own backoff, and on
 * a socket opened bare also re-runs the auth check (useAuth().refresh).
 */
export function WebSocketProvider({ children }) {
  const { authReady, authDisabled, refresh: refreshAuth } = useAuth();
  const useTicket = authReady && !authDisabled;
  // AuthContext.refresh, read at close time (see the bare 4001 case below).
  const refreshAuthRef = useRef(refreshAuth);
  useEffect(() => { refreshAuthRef.current = refreshAuth; }, [refreshAuth]);
  const wsRef = useRef(null);
  const [connected, setConnected] = useState(false);
  const reconnectTimer = useRef(null);
  const reconnectDelay = useRef(INITIAL_RECONNECT_MS);
  const pongTimer = useRef(null);
  const lastMsgAt = useRef(0);
  const connectRef = useRef(null);
  // A ticket fetch is under way: connect() and the resume handler must not
  // start a second one (wsRef is still null meanwhile).
  const ticketInFlight = useRef(false);
  // Bumped per ticket fetch and on teardown; a stale fetch result is dropped.
  const connectGen = useRef(0);
  // The server ended this session (4001 expired/revoked/idle, or the ticket
  // fetch failed auth). api.js has run the expiry flow and AuthGuard moves
  // to /login, which unmounts this provider: no more reconnects meanwhile.
  const sessionEnded = useRef(false);
  // Backoff for 4001 missing/invalid. Those sockets are accepted before the
  // close, so onopen has already reset reconnectDelay; without its own delay
  // a persistent ticket problem would reconnect every second.
  const authRetryDelay = useRef(INITIAL_RECONNECT_MS);
  // Merged viewing state: union of all agent IDs currently being viewed
  // by any component (AgentChatPage panes). Backend receives the full set.
  const viewingAgentsRef = useRef(new Set());
  // Subscriber callbacks — called synchronously for every WS event
  const subscribersRef = useRef(new Set());
  // onOpen callbacks — fired each time the WS successfully opens
  // (initial connect AND reconnects). Consumers use this to trigger
  // an HTTP resync so they can catch up on events missed while
  // disconnected.
  const openSubscribersRef = useRef(new Set());

  // Register a callback that receives every WS event.
  // Returns an unsubscribe function.
  const subscribe = useCallback((handler) => {
    subscribersRef.current.add(handler);
    return () => subscribersRef.current.delete(handler);
  }, []);

  // Register a callback that fires every time the WS opens
  // (including reconnects). Returns an unsubscribe function.
  const onOpen = useCallback((handler) => {
    openSubscribersRef.current.add(handler);
    return () => openSubscribersRef.current.delete(handler);
  }, []);

  // Send a raw message to the server (if connected)
  const _send = useCallback((data) => {
    if (wsRef.current?.readyState === WebSocket.OPEN) {
      wsRef.current.send(typeof data === "string" ? data : JSON.stringify(data));
    }
  }, []);

  // Sync the backend about which agents are currently being viewed.
  // `primary_agent_id` is the single pane the user is actively interacting
  // with (for time-tracking); may be null when all panes are idle.
  const _syncViewing = useCallback(() => {
    const agents = viewingAgentsRef.current;
    const visible = document.visibilityState === "visible";
    const ids = visible ? [...agents] : [];
    const primary = visible ? pickPrimaryAgent(agents) : null;
    _send({
      type: "viewing",
      agent_ids: ids,
      has_focus: document.hasFocus(),
      primary_agent_id: primary,
    });
  }, [_send]);

  // Exactly one pending reconnect at a time.
  const scheduleReconnect = useCallback((ms) => {
    clearTimeout(reconnectTimer.current);
    reconnectTimer.current = setTimeout(() => {
      reconnectTimer.current = null;
      connectRef.current?.();
    }, ms);
  }, []);

  // Abandon `ws` (half-open, stuck handshake, errored) and reconnect after
  // `delay`. No-op if `ws` has already been superseded by a newer socket.
  const dropSocket = useCallback((ws, delay = 0) => {
    if (wsRef.current !== ws) return;
    wsRef.current = null;
    clearTimeout(pongTimer.current);
    pongTimer.current = null;
    setConnected(false);
    try { ws.close(); } catch { /* already closing */ }
    scheduleReconnect(delay);
  }, [scheduleReconnect]);

  // Send a ping and require some frame back within PONG_TIMEOUT_MS.
  const probeLiveness = useCallback(() => {
    const ws = wsRef.current;
    if (!ws || ws.readyState !== WebSocket.OPEN) return;
    if (pongTimer.current) return; // a probe is already in flight
    const sentAt = Date.now();
    try {
      ws.send("ping");
    } catch {
      dropSocket(ws);
      return;
    }
    pongTimer.current = setTimeout(() => {
      pongTimer.current = null;
      if (wsRef.current === ws && lastMsgAt.current < sentAt) dropSocket(ws);
    }, PONG_TIMEOUT_MS);
  }, [dropSocket]);

  // Open the socket, with `ticket` when there is one. `sessionToken` is the
  // stored session the ticket was minted for: a session-ending close then
  // expires that session only, never a newer login from another tab.
  const openSocket = useCallback((ticket, sessionToken) => {
    const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
    const base = `${proto}//${window.location.host}/ws/status`;
    const url = ticket ? `${base}?ticket=${encodeURIComponent(ticket)}` : base;

    let ws;
    try {
      ws = new WebSocket(url);
    } catch (err) {
      console.warn("WebSocketProvider: connection failed, will retry:", err);
      scheduleReconnect(reconnectDelay.current);
      reconnectDelay.current = nextBackoff(reconnectDelay.current, { max: MAX_RECONNECT_MS });
      return;
    }
    wsRef.current = ws;

    const handshakeTimer = setTimeout(() => {
      if (ws.readyState !== WebSocket.CONNECTING) return;
      const delay = reconnectDelay.current;
      reconnectDelay.current = nextBackoff(delay, { max: MAX_RECONNECT_MS });
      dropSocket(ws, delay);
    }, HANDSHAKE_TIMEOUT_MS);

    ws.onopen = () => {
      clearTimeout(handshakeTimer);
      if (wsRef.current !== ws) return;
      lastMsgAt.current = Date.now();
      setConnected(true);
      reconnectDelay.current = INITIAL_RECONNECT_MS;
      _syncViewing();
      for (const fn of openSubscribersRef.current) {
        try {
          fn();
        } catch (err) {
          console.error("WebSocket onOpen subscriber error:", err);
        }
      }
    };

    ws.onmessage = (e) => {
      lastMsgAt.current = Date.now();
      // Any frame means the server kept this socket past its auth check.
      authRetryDelay.current = INITIAL_RECONNECT_MS;
      let event;
      try {
        event = JSON.parse(e.data);
      } catch {
        return; // untrusted input may not be valid JSON
      }
      if (event.type === "pong" || event.type === "ping") return;
      // Calibrate client-server clock offset from every event timestamp
      if (event.timestamp) calibrate(event.timestamp);
      // Deliver to all subscribers synchronously — each callback runs
      // for every event, bypassing React batching entirely.
      for (const fn of subscribersRef.current) {
        try {
          fn(event);
        } catch (err) {
          console.error("WebSocket subscriber error:", err);
        }
      }
    };

    ws.onclose = (ev) => {
      clearTimeout(handshakeTimer);
      // A socket we already dropped (or replaced) must not schedule a
      // second reconnect on top of the one its replacement owns.
      if (wsRef.current !== ws) return;
      wsRef.current = null;
      clearTimeout(pongTimer.current);
      pongTimer.current = null;
      setConnected(false);
      const endReason = wsSessionEndReason(ev);
      if (endReason) {
        const current = getAuthToken();
        if (sessionToken && current && current !== sessionToken) {
          // That session was already replaced by a newer login: use it.
          scheduleReconnect(0);
          return;
        }
        sessionEnded.current = true;
        expireSession(endReason, { requestToken: sessionToken, source: "ws-status" }).catch(() => {});
        return;
      }
      if (ev?.code === WS_AUTH_CLOSE_CODE) {
        // missing / invalid: the next connect fetches a fresh ticket.
        // Opened without one, the server now wants a ticket (DISABLE_AUTH
        // was flipped off under this tab): re-run the auth check so
        // AuthGuard learns the mode and this provider reconnects with one.
        if (!ticket) Promise.resolve().then(() => refreshAuthRef.current?.()).catch(() => {});
        const delay = authRetryDelay.current;
        authRetryDelay.current = nextBackoff(delay, { max: MAX_RECONNECT_MS });
        scheduleReconnect(delay);
        return;
      }
      scheduleReconnect(reconnectDelay.current);
      reconnectDelay.current = nextBackoff(reconnectDelay.current, { max: MAX_RECONNECT_MS });
    };

    ws.onerror = () => {
      try { ws.close(); } catch { /* ignore */ }
    };
  }, [_syncViewing, scheduleReconnect, dropSocket]);

  const connect = useCallback(() => {
    const cur = wsRef.current;
    if (cur && (cur.readyState === WebSocket.OPEN || cur.readyState === WebSocket.CONNECTING)) return;
    if (ticketInFlight.current || sessionEnded.current) return;
    clearTimeout(reconnectTimer.current);
    reconnectTimer.current = null;

    const sessionToken = getAuthToken();
    if (!useTicket) {
      openSocket(null, sessionToken);
      return;
    }
    const gen = ++connectGen.current;
    ticketInFlight.current = true;
    // Bounded like the handshake: a fetch that hangs (iOS resume) would
    // otherwise hold the in-flight guard forever.
    withTimeout((signal) => fetchWsTicket({ signal }), HANDSHAKE_TIMEOUT_MS).then(
      (r) => {
        if (gen !== connectGen.current) return;
        ticketInFlight.current = false;
        openSocket(r?.ticket || null, sessionToken);
      },
      (err) => {
        if (gen !== connectGen.current) return;
        ticketInFlight.current = false;
        if (err instanceof AuthError) {
          const current = getAuthToken();
          if (current && current !== sessionToken) {
            scheduleReconnect(0); // a newer login won the race
            return;
          }
          sessionEnded.current = true; // api.js already ran the expiry flow
          return;
        }
        console.warn("WebSocketProvider: ticket fetch failed, will retry:", err);
        scheduleReconnect(reconnectDelay.current);
        reconnectDelay.current = nextBackoff(reconnectDelay.current, { max: MAX_RECONNECT_MS });
      },
    );
  }, [useTicket, openSocket, scheduleReconnect]);

  // Timers call connect through a ref so scheduleReconnect/dropSocket
  // (declared above connect) never capture a stale closure.
  useEffect(() => {
    connectRef.current = connect;
  }, [connect]);

  useEffect(() => {
    sessionEnded.current = false;
    connect();

    const pingInterval = setInterval(probeLiveness, PING_INTERVAL_MS);

    // Periodic viewing resync so the backend picks up:
    //  (a) switches between split-screen panes via mouse/keyboard
    //  (b) idle-threshold transitions when the user stops interacting
    // Interval half of backend tick (10s) so the primary is fresh by the
    // time the next tick fires.
    const viewingResyncInterval = setInterval(() => {
      _syncViewing();
    }, 5000);

    const onVisibilityChange = () => {
      _syncViewing();
    };
    const onFocusChange = () => {
      _syncViewing();
    };
    document.addEventListener("visibilitychange", onVisibilityChange);
    window.addEventListener("focus", onFocusChange);
    window.addEventListener("blur", onFocusChange);

    // Foreground / network back: don't wait out the backoff. A socket that
    // still reports OPEN after an iOS resume is verified with a probe.
    const offResume = onNetworkResume(() => {
      reconnectDelay.current = INITIAL_RECONNECT_MS;
      // A ticket fetch is already under way; its socket follows.
      if (ticketInFlight.current) return;
      const ws = wsRef.current;
      if (ws && ws.readyState === WebSocket.OPEN) {
        probeLiveness();
        return;
      }
      if (ws) {
        dropSocket(ws); // CONNECTING / CLOSING: abandon it and retry now
        return;
      }
      scheduleReconnect(0);
    });

    return () => {
      clearInterval(pingInterval);
      clearInterval(viewingResyncInterval);
      clearTimeout(reconnectTimer.current);
      reconnectTimer.current = null;
      clearTimeout(pongTimer.current);
      pongTimer.current = null;
      connectGen.current += 1; // drop a ticket fetch still in flight
      ticketInFlight.current = false;
      offResume();
      document.removeEventListener("visibilitychange", onVisibilityChange);
      window.removeEventListener("focus", onFocusChange);
      window.removeEventListener("blur", onFocusChange);
      const ws = wsRef.current;
      if (ws) {
        wsRef.current = null;
        ws.close();
      }
    };
  }, [connect, _syncViewing, probeLiveness, dropSocket, scheduleReconnect]);

  // Public API for components to send messages
  const sendWsMessage = useCallback((data) => {
    if (typeof data === "object" && data.type === "viewing") {
      // Track viewing state centrally
      const agentId = data.agent_id;
      if (agentId) {
        viewingAgentsRef.current.add(agentId);
      } else {
        // null means "stop viewing" — components pass their own agent_id
        // on unmount, so we need a way to remove specific IDs.
        // Convention: { type: "viewing", agent_id: null, _unview: "xxx" }
        const toRemove = data._unview;
        if (toRemove) viewingAgentsRef.current.delete(toRemove);
      }
      _syncViewing();
      return;
    }
    _send(data);
  }, [_send, _syncViewing]);

  return (
    <WebSocketContext.Provider value={{ subscribe, onOpen, connected, sendWsMessage }}>
      {children}
    </WebSocketContext.Provider>
  );
}

// Safe fallback so components don't crash if rendered outside the provider
// (e.g. during HMR transitions or stale service-worker cache).
const _fallback = {
  subscribe: () => () => {},
  onOpen: () => () => {},
  connected: false,
  sendWsMessage: () => {},
};

export function useWebSocketContext() {
  const ctx = useContext(WebSocketContext);
  if (!ctx) {
    console.warn("useWebSocketContext: no provider found, using fallback (stale cache?)");
    return _fallback;
  }
  return ctx;
}
