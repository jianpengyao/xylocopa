import { useCallback, useEffect, useRef } from "react";
import { useWebSocketContext } from "../contexts/WebSocketContext";
export * from "../lib/notifications";

/**
 * Subscribe to WebSocket events with a callback.
 * The callback is invoked synchronously for every event — no events
 * are lost to React 18 batching.
 *
 * @param {Function} handler - Called with each WebSocket event object
 * @param {Array} deps - Extra dependencies (handler is always latest via ref)
 */
export function useWsEvent(handler, deps = []) {
  const { subscribe } = useWebSocketContext();
  const handlerRef = useRef(handler);
  handlerRef.current = handler;
  useEffect(() => {
    return subscribe((event) => handlerRef.current(event));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [subscribe, ...deps]);
}

/**
 * Shared WebSocket hook — delegates to the single WebSocketProvider connection.
 *
 * Deliberately exposes no `lastEvent`: keeping the latest event in state made
 * every caller (chat page, tasks page, task detail) re-render on every WS
 * event — several times a second during tool activity — and nothing read it.
 * Use useWsEvent() for event delivery.
 */
export default function useWebSocket() {
  const { connected, sendWsMessage: ctxSend } = useWebSocketContext();

  // Wrap sendWsMessage to handle the "viewing" convention
  const sendWsMessage = useCallback((data) => {
    ctxSend(data);
  }, [ctxSend]);

  return { connected, sendWsMessage };
}
