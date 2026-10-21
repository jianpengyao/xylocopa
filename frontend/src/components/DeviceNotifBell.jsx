import { useCallback, useEffect, useState } from "react";
import { Bell, BellOff, BellPlus } from "lucide-react";
import { useToast } from "../contexts/ToastContext";
import {
  enablePushOnThisDevice,
  getPushDeviceState,
  onPushDeviceChange,
  prefetchVapidKey,
  setThisDeviceAgentsEnabled,
  syncPushDevice,
} from "../lib/pushNotifications";

function isIOS() {
  const ua = navigator.userAgent || "";
  // iPadOS reports a Mac UA; touch points give it away.
  return /iPhone|iPad|iPod/.test(ua) || (/Macintosh/.test(ua) && navigator.maxTouchPoints > 1);
}

// Desktop Safari (not iPadOS, which isIOS covers): its per-site switch is
// not behind an address-bar icon, and a Dock web app has no address bar.
function isMacSafari() {
  const ua = navigator.userAgent || "";
  return /Macintosh/.test(ua) && /Safari\//.test(ua)
    && !/Chrome\/|Chromium\/|Edg\/|OPR\/|Firefox\//.test(ua) && !isIOS();
}

function isStandalone() {
  try {
    return navigator.standalone === true || !!window.matchMedia?.("(display-mode: standalone)").matches;
  } catch {
    return false;
  }
}

const TITLES = {
  unsupported: "Notifications aren't available in this browser",
  syncing: "Checking notifications on this device…",
  "off-device": "Turn on notifications on this device",
  denied: "Notifications are blocked on this device",
  on: "Mute notifications on this device",
  muted: "Unmute notifications on this device",
};

function unsupportedHint() {
  return isIOS()
    ? "Add Xylocopa to your Home Screen to get notifications"
    : "This browser can't show notifications";
}

function deniedHint() {
  const blocked = "Notifications are blocked on this device.";
  if (isIOS()) return `${blocked} Allow them in Settings › Notifications › Xylocopa`;
  if (isMacSafari()) {
    return isStandalone()
      ? `${blocked} Allow them in System Settings › Notifications › Xylocopa`
      : `${blocked} Allow them in Safari › Settings › Websites › Notifications`;
  }
  return `${blocked} Allow notifications for this site in your browser's site settings`;
}

/**
 * Header bell: this device's notification control. It never changes
 * another device — each device's switches live on its own subscription.
 */
export default function DeviceNotifBell() {
  const toast = useToast();
  const [state, setState] = useState(() => getPushDeviceState().state);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    const refresh = () => setState(getPushDeviceState().state);
    const unsubscribe = onPushDeviceChange(refresh);
    const { permission, device, state: now } = getPushDeviceState();
    // Granted but this device's switches are unknown (empty cache, startup
    // sync not run or failed): ask the server instead of showing "off".
    if (permission === "granted" && !device) syncPushDevice();
    // Fetched ahead so a tap reaches requestPermission() with nothing to await.
    else if (now === "off-device") prefetchVapidKey().catch(() => {});
    refresh();
    return unsubscribe;
  }, []);

  const handleClick = useCallback(() => {
    if (busy || state === "syncing") return;
    if (state === "unsupported") {
      toast.info(unsupportedHint(), 5000);
      return;
    }
    if (state === "denied") {
      toast.warning(deniedHint(), 6000);
      return;
    }
    if (state === "off-device") {
      // Synchronous on purpose: requestPermission() must run inside this
      // click (iOS standalone PWAs refuse it otherwise).
      const pending = enablePushOnThisDevice();
      setBusy(true);
      pending
        .then(({ ok, permission }) => {
          if (ok) toast.success("Notifications on for this device");
          else if (permission === "denied") toast.warning(deniedHint(), 6000);
          else if (permission === "granted") toast.error("Couldn't turn on notifications on this device");
        })
        .finally(() => setBusy(false));
      return;
    }
    const enable = state === "muted";
    setBusy(true);
    setThisDeviceAgentsEnabled(enable)
      .then(() => toast.success(enable ? "Notifications unmuted on this device" : "Notifications muted on this device"))
      .catch((err) => {
        console.warn("[push] device update failed:", err);
        toast.error("Couldn't update notifications on this device");
      })
      .finally(() => setBusy(false));
  }, [busy, state, toast]);

  const Icon = state === "on" || state === "syncing" ? Bell : state === "off-device" ? BellPlus : BellOff;
  // Blocked/unavailable (tap only explains) is dimmed, unlike a plain mute.
  const tone = state === "on" ? "text-accent"
    : state === "unsupported" || state === "denied" ? "text-dim opacity-40"
    : state === "syncing" ? "text-dim opacity-60"
    : "text-dim";

  return (
    <button
      type="button"
      onClick={handleClick}
      title={TITLES[state]}
      aria-label={TITLES[state]}
      aria-busy={busy || state === "syncing" || undefined}
      data-state={state}
      className={`w-8 h-8 flex items-center justify-center rounded-lg hover:bg-input transition-colors ${tone}`}
    >
      <Icon className="w-4 h-4" />
    </button>
  );
}
