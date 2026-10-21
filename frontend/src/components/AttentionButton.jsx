import { useNavigate, useLocation } from "react-router-dom";
import { useCallback, useEffect, useRef, useState, lazy, Suspense } from "react";
import DraggableFab from "./DraggableFab";
import AttentionOrb from "./AttentionOrb";
import { useUnread } from "../contexts/UnreadContext";
import { useWebSocketContext } from "../contexts/WebSocketContext";
import { fetchAttentionJobs } from "../lib/api";
import { loadTranscript, makeMsg, saveTranscript } from "./attention/chatStore";
import { getCharacter, subscribeCharacter } from "./attention/characterStore";
import { forwardState } from "../lib/nav";
import { getOrbEnabled, ORB_EVENT } from "../lib/orbMode";
import UnreadFab from "./UnreadFab";

// The bubble pulls in the whole chat UI; keep it out of the initial bundle
// since this button is mounted on every route.
const AttentionBubble = lazy(() => import("./attention/AttentionBubble"));

const defaultPos = () => ({
  x: window.innerWidth - 64,
  y: window.innerHeight - 140,
});

// Pending-job poll. Slow on purpose: the count only decorates the orb, so
// it does not need the 5s cadence the agent lists use.
const JOB_POLL_MS = 30000;

// How long the ball keeps its talking face after a job fires.
const SPEAK_MS = 2600;

// Kill switch (Monitor > Display > "Assistant character"): with the orb
// off, the classic unread FAB renders instead and none of the orb hooks
// mount. Separate wrapper component so the flag flip swaps whole subtrees
// without violating hook ordering.
export default function AttentionButton() {
  const [orbOn, setOrbOn] = useState(getOrbEnabled);
  useEffect(() => {
    const onChange = () => setOrbOn(getOrbEnabled());
    window.addEventListener(ORB_EVENT, onChange);
    return () => window.removeEventListener(ORB_EVENT, onChange);
  }, []);
  return orbOn ? <OrbAttentionButton /> : <UnreadFab />;
}

function OrbAttentionButton() {
  const navigate = useNavigate();
  const location = useLocation();
  const { list, total } = useUnread();
  const { subscribe } = useWebSocketContext();
  const [open, setOpen] = useState(null);        // null | "chat" | "toast"
  const [anchor, setAnchor] = useState(null);    // FAB rect snapshot
  const [syncKey, setSyncKey] = useState(0);     // bubble re-reads transcript
  const [jobCount, setJobCount] = useState(0);
  const [dragging, setDragging] = useState(false);
  const [busy, setBusy] = useState(false);       // a chat turn is in flight
  const [celebrating, setCelebrating] = useState(false);
  const [speaking, setSpeaking] = useState(false);
  const [gesture, setGesture] = useState(null);  // one-shot orb animation
  const [gestureKey, setGestureKey] = useState(0);
  const [canTrack, setCanTrack] = useState(false);
  const [character, setCharacterState] = useState(getCharacter);

  const fabElRef = useRef(null);
  const speakTimer = useRef(null);
  const celebrateTimer = useRef(null);

  // The picker (inside the bubble) changes the skin; re-render the FAB.
  useEffect(() => subscribeCharacter(() => setCharacterState(getCharacter())), []);

  // Hide on split screen page itself and on login
  const hidden = location.pathname === "/split" || location.pathname === "/login";

  const hasUnread = total > 0 && list.length > 0;

  const hop = useCallback(() => {
    setGesture("hop");
    setGestureKey((k) => k + 1);
  }, []);

  const measureAnchor = useCallback(() => {
    const el = fabElRef.current;
    if (!el) return null;
    const r = el.getBoundingClientRect();
    const rect = {
      left: r.left, top: r.top, right: r.right, bottom: r.bottom,
      width: r.width, height: r.height,
    };
    setAnchor(rect);
    return rect;
  }, []);

  // The FAB is pinned to the viewport edges, so its rect shifts on resize
  // while the bubble is open — keep the anchor honest.
  useEffect(() => {
    if (!open) return undefined;
    const onResize = () => measureAnchor();
    window.addEventListener("resize", onResize);
    return () => window.removeEventListener("resize", onResize);
  }, [open, measureAnchor]);

  // ── Pupil tracking (desktop) ──
  // Event-driven, not a loop: each pointermove schedules exactly one rAF
  // that writes two CSS vars on the FAB element; the SVG pupils inherit
  // them. Coarse pointers, reduced motion and e-ink instead get the CSS
  // `orb-wander` glances.
  useEffect(() => {
    if (hidden) return undefined;
    const fine = window.matchMedia?.("(pointer: fine)")?.matches;
    const reduced = window.matchMedia?.("(prefers-reduced-motion: reduce)")?.matches;
    const eink = document.documentElement.classList.contains("eink");
    const enabled = Boolean(fine && !reduced && !eink);
    setCanTrack(enabled);
    if (!enabled) return undefined;

    let raf = 0;
    let px = 0;
    let py = 0;
    const apply = () => {
      raf = 0;
      const el = fabElRef.current;
      if (!el) return;
      const r = el.getBoundingClientRect();
      const dx = px - (r.left + r.width / 2);
      const dy = py - (r.top + r.height / 2);
      const d = Math.hypot(dx, dy) || 1;
      // Full deflection ~140px out; nearer, the eyes converge back toward
      // centre — like focusing on a finger approaching your nose.
      const m = Math.min(1, d / 140) * 2.4;
      // Downward travel is capped tighter than the rest: the eyes sit just
      // above the mouth, and at full deflection they collide with it.
      const ty = Math.min((dy / d) * m, 0.6);
      el.style.setProperty("--orb-px", `${((dx / d) * m).toFixed(2)}px`);
      el.style.setProperty("--orb-py", `${ty.toFixed(2)}px`);
    };
    const onMove = (e) => {
      px = e.clientX;
      py = e.clientY;
      if (!raf) raf = requestAnimationFrame(apply);
    };
    window.addEventListener("pointermove", onMove, { passive: true });
    return () => {
      window.removeEventListener("pointermove", onMove);
      if (raf) cancelAnimationFrame(raf);
      fabElRef.current?.style.removeProperty("--orb-px");
      fabElRef.current?.style.removeProperty("--orb-py");
    };
  }, [hidden]);

  // ── Job-count poll ── skipped while the bubble is open (it owns the
  // authoritative list) and while hidden.
  useEffect(() => {
    if (hidden || open) return undefined;
    let alive = true;
    // Rendered only inside AuthGuard: no "token stored" gate (none is stored
    // under DISABLE_AUTH).
    const poll = () => {
      fetchAttentionJobs()
        .then((r) => {
          if (!alive) return;
          const active = (r.jobs || []).filter((j) => j.status === "active");
          setJobCount(active.length);
        })
        .catch(() => { /* orb decoration only — never surface this */ });
    };
    poll();
    const id = setInterval(poll, JOB_POLL_MS);
    return () => { alive = false; clearInterval(id); };
  }, [hidden, open, location.pathname]);

  // ── A job fired: the ball speaks it ──
  // The message goes into the transcript store first (the bubble may not be
  // mounted), then the bubble is opened in toast mode — or, if already
  // open, just told to re-read. This is the light in-app path; the OS push
  // still covers the tab-in-background case.
  useEffect(() => {
    if (hidden) return undefined;
    return subscribe((event) => {
      if (event?.type !== "attention_fired") return;
      const d = event.data || {};
      const msgs = loadTranscript();
      msgs.push(makeMsg("assistant", d.body || d.title || "Done.", {
        fired: true,
        title: d.title && d.title !== d.body ? d.title : undefined,
        url: d.url,
      }));
      saveTranscript(msgs);
      setSyncKey((k) => k + 1);
      setOpen((cur) => cur || "toast");
      measureAnchor();
      hop();
      setSpeaking(true);
      clearTimeout(speakTimer.current);
      speakTimer.current = setTimeout(() => setSpeaking(false), SPEAK_MS);
    });
  }, [hidden, subscribe, measureAnchor, hop]);

  useEffect(() => () => {
    clearTimeout(speakTimer.current);
    clearTimeout(celebrateTimer.current);
  }, []);

  const handleTap = useCallback(() => {
    if (open) {
      setOpen(null);
      return;
    }
    // A tap NEVER navigates — it only toggles the bubble. It used to jump
    // to the oldest unread chat, which silently swapped the agent under a
    // user mid-conversation and caused a real misdelivery (2026-07-29:
    // message re-sent into the wrong agent after the surprise switch —
    // the chat page is the same component for every agent, so the swap is
    // invisible). Unread is one tap further: the bubble's "waiting" chip.
    measureAnchor();
    setOpen("chat");
    hop();
  }, [open, measureAnchor, hop]);

  const handleLongPress = useCallback(() => {
    // Always open split-screen, even with unread messages (escape hatch)
    setOpen(null);
    navigate("/split", { state: { initialPath: location.pathname } });
  }, [navigate, location.pathname]);

  const openAgent = useCallback((id) => {
    navigate(`/agents/${id}`, { state: forwardState(location) });
  }, [navigate, location]);

  const handleDragChange = useCallback((d) => {
    setDragging(d);
    // The bubble is anchored to where the ball was — moving the ball with
    // it open would leave the tail pointing at nothing.
    if (d) setOpen(null);
  }, []);

  const handleCelebrate = useCallback(() => {
    hop();
    setCelebrating(true);
    clearTimeout(celebrateTimer.current);
    celebrateTimer.current = setTimeout(() => setCelebrating(false), 1400);
  }, [hop]);

  if (hidden) return null;

  // Dragging wins (feedback about the gesture in progress), then the
  // transient conversational states, then the standing ones. While the
  // chat is open and quiet the ball beams (😊) at the user — closed
  // smiling eyes, so pupil tracking pauses on purpose.
  const mood = dragging ? "dragging"
    : speaking || open === "toast" ? "speak"
      : busy ? "thinking"
        : celebrating || open === "chat" ? "done"
          : hasUnread ? "unread"
            : "idle";

  // The badge counts unread first (it is what a tap acts on); pending jobs
  // only surface when there is no unread, so the number always matches what
  // the gesture will do.
  const badge = hasUnread
    ? (total > 99 ? "99+" : String(total))
    : jobCount > 0 ? String(jobCount) : null;

  const label = hasUnread
    ? `Assistant — ${total} unread message${total === 1 ? "" : "s"}, tap to open, long-press for split screen`
    : jobCount > 0
      ? `Assistant — ${jobCount} pending job${jobCount === 1 ? "" : "s"}`
      : "Assistant — tap to chat, long-press for split screen";

  return (
    <>
      <DraggableFab
        storageKey="ah:fab-pos-split-v3"
        defaultPosition={defaultPos}
        onClick={handleTap}
        onLongPress={handleLongPress}
        onDragChange={handleDragChange}
        outerRef={fabElRef}
        ariaLabel={label}
        className="w-11 h-11 flex items-center justify-center rounded-full transition-transform active:scale-90"
      >
        <AttentionOrb
          mood={mood}
          badge={badge}
          character={character}
          gesture={gesture}
          gestureKey={gestureKey}
          wander={!canTrack}
          className="w-11 h-11"
        />
      </DraggableFab>

      {open && anchor && (
        <Suspense fallback={null}>
          <AttentionBubble
            anchor={anchor}
            variant={open}
            fabRef={fabElRef}
            syncKey={syncKey}
            jobCount={jobCount}
            unreadList={list}
            onOpenAgent={openAgent}
            onExpand={() => setOpen("chat")}
            onClose={() => setOpen(null)}
            onBusyChange={setBusy}
            onCelebrate={handleCelebrate}
          />
        </Suspense>
      )}
    </>
  );
}
