import { createContext, useContext, useEffect, useRef, useCallback, useState } from "react";
import { getAuthToken } from "../lib/api";
import { calibrate } from "../lib/serverTime";
import { pickPrimaryAgent } from "../lib/notifications";
import {
  nextBackoff,
  onNetworkResume,
  WS_PING_INTERVAL_MS as PING_INTERVAL_MS,
  WS_PONG_TIMEOUT_MS as PONG_TIMEOUT_MS,
  WS_HANDSHAKE_TIMEOUT_MS as HANDSHAKE_TIMEOUT_MS,
  WS_INITIAL_RECONNECT_MS as INITIAL_RECONNECT_MS,
  WS_MAX_RECONNECT_MS as MAX_RECONNECT_MS,
} from "../lib/reconnect";

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
 */
export function WebSocketProvider({ children }) {
  const wsRef = useRef(null);
  const [connected, setConnected] = useState(false);
  const reconnectTimer = useRef(null);
  const reconnectDelay = useRef(INITIAL_RECONNECT_MS);
  const pongTimer = useRef(null);
  const lastMsgAt = useRef(0);
  const connectRef = useRef(null);
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

  const connect = useCallback(() => {
    const cur = wsRef.current;
    if (cur && (cur.readyState === WebSocket.OPEN || cur.readyState === WebSocket.CONNECTING)) return;
    clearTimeout(reconnectTimer.current);
    reconnectTimer.current = null;

    const token = getAuthToken();

    const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
    const url = token
      ? `${proto}//${window.location.host}/ws/status?token=${encodeURIComponent(token)}`
      : `${proto}//${window.location.host}/ws/status`;

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

    ws.onclose = () => {
      clearTimeout(handshakeTimer);
      // A socket we already dropped (or replaced) must not schedule a
      // second reconnect on top of the one its replacement owns.
      if (wsRef.current !== ws) return;
      wsRef.current = null;
      clearTimeout(pongTimer.current);
      pongTimer.current = null;
      setConnected(false);
      scheduleReconnect(reconnectDelay.current);
      reconnectDelay.current = nextBackoff(reconnectDelay.current, { max: MAX_RECONNECT_MS });
    };

    ws.onerror = () => {
      try { ws.close(); } catch { /* ignore */ }
    };
  }, [_syncViewing, scheduleReconnect, dropSocket]);

  // Timers call connect through a ref so scheduleReconnect/dropSocket
  // (declared above connect) never capture a stale closure.
  useEffect(() => {
    connectRef.current = connect;
  }, [connect]);

  useEffect(() => {
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
