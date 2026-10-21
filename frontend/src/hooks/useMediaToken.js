import { useSyncExternalStore } from "react";
import { getMediaToken, subscribeMediaToken } from "../lib/api";

/**
 * The stored media token (lib/api.js), or null. Re-renders the caller when
 * AuthGuard (or another tab) re-mints it, so components holding memoized
 * media URLs can re-stamp them (lib/urls.js withCurrentMediaToken).
 */
export default function useMediaToken() {
  return useSyncExternalStore(subscribeMediaToken, getMediaToken, () => null);
}
