import { useState, useEffect } from "react";
import { authedFetch } from "../lib/api";

const detectPlatform = () => {
  if (typeof navigator === "undefined") return "ios";
  const ua = navigator.userAgent;
  if (/Android/i.test(ua)) return "android";
  if (/iPhone|iPad|iPod/i.test(ua)) return "ios";
  return "ios";
};

const iosCertSteps = [
  {
    num: 1,
    title: "Download certificate",
    desc: "",
    link: { href: "/api/cert", label: "Tap here to download" },
  },
  {
    num: 2,
    title: "Install profile",
    desc: "Settings → General → VPN & Device Management → tap the downloaded profile → Install.",
  },
  {
    num: 3,
    title: "Enable full trust (required)",
    desc: "Settings → General → About → Certificate Trust Settings → under \"Enable full trust for root certificates\", turn on \"mkcert\" or \"xylocopa\" and tap Continue.",
    // Installing the profile does not make iOS trust it for HTTPS. Without
    // this switch the app only loads on Safari's per-site "visit this
    // website" exception, which iOS can drop without notice; every load is
    // then refused (TLS alert certificate_unknown) until the exception is
    // granted again. Seen on an iPhone that ran this way for months.
    note: "Installing the profile is not enough. Without this switch the app only works on Safari's temporary \"visit this website\" exception, and it stops loading whenever iOS drops that exception.",
  },
];

const iosPwaSteps = [
  {
    num: 1,
    title: "Open Share menu",
    desc: "Tap the Share button (square with up-arrow) at the bottom of Safari.",
  },
  {
    num: 2,
    title: "Add to Home Screen",
    desc: "Scroll down in the Share sheet, tap \"Add to Home Screen\", then tap \"Add\".",
  },
  {
    num: 3,
    title: "Done!",
    desc: "The Xylocopa icon appears on your Home Screen. Tap it and set your password.",
  },
];

const androidCertSteps = [
  {
    num: 1,
    title: "Download certificate",
    desc: "",
    link: { href: "/api/cert", label: "Tap here to download" },
  },
  {
    num: 2,
    title: "Set a screen lock first (if you haven't)",
    desc: "Android won't let you install a CA without a PIN, pattern, or password. Settings → Security → Screen lock.",
  },
  {
    num: 3,
    title: "Install certificate",
    desc: "Settings → Security → Encryption & credentials → Install a certificate → CA certificate → pick the downloaded file. Path varies by ROM — if you can't find it, search Settings for \"certificate\".",
  },
  {
    num: 4,
    title: "Confirm trust warning",
    desc: "Tap \"Install anyway\" — Android warns that user-installed CAs are less trusted; that's expected.",
  },
];

const androidPwaSteps = [
  {
    num: 1,
    title: "Open Chrome menu",
    desc: "Tap ⋮ (three dots, top right of Chrome).",
  },
  {
    num: 2,
    title: "Install app",
    desc: "Tap \"Install app\" or \"Add to Home screen\". Confirm.",
  },
  {
    num: 3,
    title: "Done!",
    desc: "The Xylocopa icon appears on your Home Screen. The bee icon loads from a public CDN, so it shows up correctly even though the server uses a private certificate.",
  },
];

// Sticky flag so AuthGuard knows we're mid-cert-install and should bounce
// the user back to /cert-guide instead of /login if the cert download
// causes a same-tab navigation away (PWA WebViews especially).
// Consumed once and TTL'd to 60s so it can't pollute normal navigation.
export const CERT_FLOW_KEY = "xy:cert-flow-pending";
export const CERT_FLOW_TTL_MS = 60_000;

// Pre-fills with the host the browser is currently using (window.location.hostname)
// — that's the server identity from this client's vantage point, which is exactly
// what the cert SAN needs to cover. User confirms, server re-signs with just that
// one IP. ensure-cert.sh always adds 127.0.0.1 + DNS names server-side.
function CertRegenSection() {
  const [ip, setIp] = useState(typeof window !== "undefined" ? window.location.hostname : "");
  const [regenerating, setRegenerating] = useState(false);
  const [msg, setMsg] = useState(null);
  const [coveredIps, setCoveredIps] = useState(null);  // null=loading, [] on err

  // Pull the live SAN so we can tell the user up-front whether the typed
  // IP is already covered (button becomes a no-op).  Re-fetch after a real
  // regen completes so the badge updates.
  const refreshSan = () => {
    fetch("/api/cert/info")
      .then((r) => r.json())
      .then((j) => setCoveredIps(Array.isArray(j?.ips) ? j.ips : []))
      .catch(() => setCoveredIps([]));
  };
  useEffect(refreshSan, []);

  const isCovered = coveredIps !== null && coveredIps.includes(ip.trim());

  const handleRegenerate = async () => {
    const cleaned = ip.trim();
    if (!/^[0-9.]+$/.test(cleaned)) {
      setMsg({ kind: "error", text: "Enter a valid IP first" });
      return;
    }
    setRegenerating(true);
    setMsg(null);
    try {
      // Bearer if this browser is signed in. authedFetch has no 401 side
      // effects: this page is public (no AuthGuard), so a 401 is shown here
      // and never routed into the session-expiry flow.
      const r = await authedFetch("/api/cert/regenerate", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ips: [cleaned], dns: ["xylocopa", "localhost"] }),
      });
      if (r.status === 401) {
        setMsg({ kind: "signin" });
        return;
      }
      const j = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(j.detail || `HTTP ${r.status}`);
      // Backend skipped the regen because the current cert already covers
      // this IP — no leaf fingerprint change, no Safari trust loss, no
      // reload needed.  This is the happy path on repeat clicks.
      if (j.skipped) {
        setMsg({ kind: "ok", text: "Already covered — no regen needed." });
        refreshSan();
      } else {
        setMsg({ kind: "ok", text: "Done. Reloading…" });
        setTimeout(() => window.location.reload(), 2000);
      }
    } catch (e) {
      setMsg({ kind: "error", text: String(e.message || e) });
    } finally {
      setRegenerating(false);
    }
  };

  return (
    <div className="space-y-4">
      <div className="flex gap-3">
        <div className="flex-shrink-0 w-7 h-7 rounded-full bg-accent text-white text-sm font-bold flex items-center justify-center">
          1
        </div>
        <div className="min-w-0 flex-1">
          <div className="font-medium text-heading text-sm">Confirm IP address</div>
          <p className="text-xs text-dim mt-0.5">The address you're using to access this app.</p>
          <input
            type="text"
            value={ip}
            onChange={(e) => setIp(e.target.value)}
            className="mt-2 w-full font-mono text-xs bg-page/50 border border-divider/50 rounded-md px-2 py-1.5 text-heading"
            spellCheck={false}
          />
          {coveredIps !== null && ip.trim() && (
            <p className={`text-xs mt-1.5 ${isCovered ? "text-emerald-400" : "text-amber-400"}`}>
              {isCovered ? "✓ Already in cert" : "⚠ Not in cert yet — click Regenerate to add"}
            </p>
          )}
        </div>
      </div>

      <div className="flex gap-3">
        <div className="flex-shrink-0 w-7 h-7 rounded-full bg-accent text-white text-sm font-bold flex items-center justify-center">
          2
        </div>
        <div className="min-w-0 flex-1">
          <div className="font-medium text-heading text-sm">Regenerate certificate</div>
          <p className="text-xs text-dim mt-0.5">Re-issues the cert for this IP only.</p>
          <button
            type="button"
            onClick={handleRegenerate}
            disabled={regenerating || isCovered}
            className="mt-2 px-3 py-1.5 rounded-md text-xs bg-accent hover:opacity-90 disabled:opacity-40 disabled:cursor-not-allowed text-white"
          >
            {regenerating ? "Regenerating…" : isCovered ? "No regen needed" : "Regenerate"}
          </button>
          {msg?.kind === "signin" ? (
            <p className="text-xs mt-2 text-danger">
              Sign in first to regenerate the certificate.{" "}
              <a href="/login" className="text-accent underline">Sign in</a>
            </p>
          ) : msg && (
            <p className={`text-xs mt-2 ${msg.kind === "ok" ? "text-ok" : "text-danger"}`}>
              {msg.text}
            </p>
          )}
        </div>
      </div>
    </div>
  );
}

function StepList({ steps, onLinkClick }) {
  return (
    <div className="space-y-4">
      {steps.map((s) => (
        <div key={s.num} className="flex gap-3">
          <div className="flex-shrink-0 w-7 h-7 rounded-full bg-accent text-white text-sm font-bold flex items-center justify-center">
            {s.num}
          </div>
          <div className="min-w-0">
            <div className="font-medium text-heading text-sm">{s.title}</div>
            <p className="text-xs text-dim mt-0.5">
              {s.desc}
              {s.link && (
                <>
                  {" "}
                  <a
                    href={s.link.href}
                    onClick={onLinkClick}
                    className="text-accent underline"
                  >
                    {s.link.label}
                  </a>
                  .
                </>
              )}
            </p>
            {s.note && (
              <p className="text-xs mt-1.5 rounded-lg px-2.5 py-1.5 bg-amber-500/10 text-amber-700 dark:text-amber-400">
                {s.note}
              </p>
            )}
          </div>
        </div>
      ))}
    </div>
  );
}

export default function CertGuidePage() {
  const [platform, setPlatform] = useState(detectPlatform);

  // We're back on cert-guide — clear any pending flag from a prior cert click.
  useEffect(() => {
    sessionStorage.removeItem(CERT_FLOW_KEY);
  }, []);

  const handleCertLinkClick = () => {
    sessionStorage.setItem(CERT_FLOW_KEY, String(Date.now()));
  };

  // Capture Chrome's beforeinstallprompt so the Android PWA install button
  // can fire the native install dialog directly. May or may not fire
  // depending on engagement heuristics — manual menu instructions stay
  // visible as a fallback.
  const [installPrompt, setInstallPrompt] = useState(null);
  useEffect(() => {
    const handler = (e) => {
      e.preventDefault();
      setInstallPrompt(e);
    };
    window.addEventListener("beforeinstallprompt", handler);
    return () => window.removeEventListener("beforeinstallprompt", handler);
  }, []);

  const handleAndroidInstall = async () => {
    if (!installPrompt) return;
    installPrompt.prompt();
    try {
      await installPrompt.userChoice;
    } finally {
      setInstallPrompt(null);
    }
  };

  const isAndroid = platform === "android";

  return (
    // Top-aligned scrollable container.  We deliberately do NOT use
    // `flex items-center justify-center` on the scroller: with tall content
    // (this page has 3 step sections), vertical centering pushes the top
    // off-screen and you can't scroll back up.  Backdrop is `fixed` +
    // `pointer-events-none` so it covers the whole viewport regardless of
    // scroll position and doesn't intercept taps.
    <div className="fixed inset-0 z-50 overflow-y-auto">
      <div className="fixed inset-0 bg-page/60 backdrop-blur-2xl pointer-events-none" />

      <div
        className="relative z-10 w-full max-w-sm mx-auto px-4"
        style={{
          // Respect iOS safe areas (notch / Dynamic Island / home indicator).
          // viewport-fit=cover is set in index.html, so env() returns non-zero.
          paddingTop: "max(2rem, env(safe-area-inset-top))",
          paddingBottom: "max(2rem, env(safe-area-inset-bottom))",
        }}
      >
        {/* Platform toggle — UA detection isn't always right (e.g. desktop Chrome
            request-mobile-site, or testing iOS flow on Android), let users override. */}
        <div className="flex justify-center mb-4 gap-1 text-xs">
          <button
            type="button"
            onClick={() => setPlatform("ios")}
            className={`px-3 py-1 rounded-full transition-colors ${platform === "ios" ? "bg-accent text-white" : "bg-surface/50 text-dim hover:text-heading"}`}
          >
            iOS
          </button>
          <button
            type="button"
            onClick={() => setPlatform("android")}
            className={`px-3 py-1 rounded-full transition-colors ${platform === "android" ? "bg-accent text-white" : "bg-surface/50 text-dim hover:text-heading"}`}
          >
            Android
          </button>
        </div>

        {/* Step 1: Regenerate cert for the current IP (only needed if the
            host's address changed since the last cert was signed). */}
        <div className="text-center mb-5">
          <h1 className="text-lg font-semibold text-heading">Step 1: Regenerate Certificate</h1>
          <p className="text-sm text-dim mt-1">Sign a fresh cert for the address you're using</p>
        </div>

        <div className="rounded-2xl bg-surface/60 backdrop-blur-md border border-divider/50 p-5 shadow-lg">
          <CertRegenSection />
        </div>

        {/* Section 2: CA Certificate (must be done first) */}
        <div className="text-center mt-8 mb-5">
          <h1 className="text-lg font-semibold text-heading">Step 2: Trust Certificate</h1>
          <p className="text-sm text-dim mt-1">Required for voice input, file uploads, and the app to work without warnings</p>
        </div>

        <div className="rounded-2xl bg-surface/60 backdrop-blur-md border border-divider/50 p-5 shadow-lg">
          <StepList
            steps={isAndroid ? androidCertSteps : iosCertSteps}
            onLinkClick={handleCertLinkClick}
          />
        </div>

        {/* Section 3: Add to Home Screen */}
        <div className="text-center mt-8 mb-5">
          <h2 className="text-base font-semibold text-heading">Step 3: Add to Home Screen</h2>
          <p className="text-sm text-dim mt-1">
            {isAndroid ? "Install as a PWA from Chrome" : "Install as a PWA from Safari"}
          </p>
        </div>

        <div className="rounded-2xl bg-surface/60 backdrop-blur-md border border-divider/50 p-5 shadow-lg">
          {isAndroid ? (
            <>
              <StepList steps={androidPwaSteps} />

              {/* If Chrome already fired beforeinstallprompt, give a one-tap
                  install button. Otherwise the user follows the manual
                  steps above. */}
              {installPrompt && (
                <button
                  type="button"
                  onClick={handleAndroidInstall}
                  className="mt-4 w-full py-3 rounded-xl font-medium bg-accent hover:opacity-90 active:scale-[0.98] text-white transition-all"
                >
                  Install Xylocopa App
                </button>
              )}
            </>
          ) : (
            <StepList steps={iosPwaSteps} />
          )}
        </div>

        <p className="text-center mt-4">
          <a href="/login" className="text-sm text-accent hover:underline">
            Back to login
          </a>
        </p>
      </div>
    </div>
  );
}
