import { useCallback, useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { useNavigate } from "react-router-dom";

import {
  chatAttention,
  createAttentionJob,
  deleteAttentionJob,
  fetchAttentionJobs,
  generateAttentionCharacter,
  patchAttentionJob,
  runAttentionJobNow,
  snoozeAttentionJob,
} from "../../lib/api";
import AttentionOrb from "../AttentionOrb";
import {
  DEFAULT_CHARACTER,
  PRESET_CHARACTERS,
  deleteSavedCharacter,
  getCharacter,
  listSavedCharacters,
  saveGeneratedCharacter,
  setCharacter,
} from "./characterStore";
import JobRow from "./JobRow";
import { PRESETS, presetSpec, absoluteTime, relativeTime } from "./jobKinds";
import { loadTranscript, makeMsg, saveTranscript } from "./chatStore";
import { designPx } from "../../lib/textScale";

/**
 * The assistant's speech bubble — a light popover anchored to the orb,
 * tail pointing at it, per the user's sketch. Two variants:
 *
 *   chat   the full conversation: transcript + composer. Job proposals
 *          come back inline with Create/Cancel under the assistant's
 *          sentence; the job list hides behind a small pill.
 *   toast  one assistant message (a job just fired), auto-dismissing.
 *          Deliberately NOT a card system: it is the same bubble, shrunk
 *          to the one line the ball is "saying".
 *
 * No backdrop, no blur, no modal focus trap — Esc, ✕, or clicking
 * elsewhere closes it. The heavy bottom-sheet this replaces is exactly
 * what the feedback asked to remove.
 */

const GAP = 12;      // bubble ↔ ball
const MARGIN = 10;   // bubble ↔ viewport edge
const HISTORY_SENT = 12;  // turns sent to the model per message

function computeLayout(anchor, variant) {
  const vw = window.innerWidth;
  const vh = window.innerHeight;
  const width = Math.min(variant === "toast" ? 300 : 344, vw - 2 * MARGIN);
  const fabCx = anchor.left + anchor.width / 2;
  // Compose like the sketch: bubble to the upper-left of the ball, tail on
  // its right end — unless the ball was dragged somewhere that flips it.
  const above = anchor.top > vh * 0.45;
  let left = fabCx + 26 - width;
  left = Math.max(MARGIN, Math.min(left, vw - width - MARGIN));
  const tailX = Math.max(22, Math.min(fabCx - left, width - 22));
  if (above) {
    const bottom = vh - anchor.top + GAP;
    return {
      width, left, bottom, above, tailX,
      maxHeight: Math.min(480, vh - bottom - MARGIN - 4),
    };
  }
  const top = anchor.bottom + GAP;
  return {
    width, left, top, above, tailX,
    maxHeight: Math.min(480, vh - top - MARGIN),
  };
}

const firstRunLine = (iso, now = Date.now()) =>
  iso ? `${absoluteTime(iso, now)} · ${relativeTime(iso, now)}` : null;

// Starter chips shown on an empty transcript — each prefills the composer
// so the user edits instead of composing from scratch.
const STARTERS = [
  "Remind me in 1h to …",
  "Ping me when my agent finishes",
  "Every weekday at 9am, send me an agent digest",
];

export default function AttentionBubble({
  anchor,             // FAB getBoundingClientRect() snapshot
  variant = "chat",   // "chat" | "toast"
  fabRef,             // outside-click must ignore the ball (it toggles itself)
  syncKey = 0,        // bumped by the parent after it appends a fired message
  jobCount = 0,
  unreadList = [],
  onOpenAgent,
  onExpand,           // toast → full chat
  onClose,
  onBusyChange,       // orb goes "thinking" while a turn is in flight
  onCelebrate,        // orb hops when a job lands
}) {
  const navigate = useNavigate();
  const [msgs, setMsgs] = useState(loadTranscript);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [view, setView] = useState("chat");       // "chat" | "jobs" | "face"
  const [activeChar, setActiveChar] = useState(getCharacter);
  const [savedChars, setSavedChars] = useState(listSavedCharacters);
  const [charPrompt, setCharPrompt] = useState("");
  const [charBusy, setCharBusy] = useState(false);
  const [charPreview, setCharPreview] = useState(null);
  const [charError, setCharError] = useState(null);
  const [jobs, setJobs] = useState(null);         // null = not yet loaded
  const [confirmingId, setConfirmingId] = useState(null);
  const [layoutTick, setLayoutTick] = useState(0);
  // Keyboard state: open flag, px of layout viewport the keyboard covers
  // (pan-corrected), and the visual viewport height to clamp against.
  const [kb, setKb] = useState({ open: false, offset: 0, vvh: 0 });

  const bubbleRef = useRef(null);
  const scrollRef = useRef(null);
  const inputRef = useRef(null);
  const toastTimer = useRef(null);

  const setBusyBoth = useCallback((b) => {
    setBusy(b);
    onBusyChange?.(b);
  }, [onBusyChange]);

  const commitMsgs = useCallback((updater) => {
    setMsgs((prev) => {
      const next = typeof updater === "function" ? updater(prev) : updater;
      saveTranscript(next);
      return next;
    });
  }, []);

  // The parent appends fired messages straight to the store (the bubble
  // may not even be mounted when a job fires) — re-read on its signal.
  useEffect(() => {
    if (syncKey > 0) setMsgs(loadTranscript());
  }, [syncKey]);

  // ── Positioning ──
  useEffect(() => {
    const onResize = () => setLayoutTick((t) => t + 1);
    window.addEventListener("resize", onResize);
    return () => window.removeEventListener("resize", onResize);
  }, []);

  // ── Mobile keyboard ──
  // Same pattern as AgentChatPage's composer (the app's one proven answer
  // to iOS keyboards): detect the keyboard from innerHeight − vv.height,
  // compute the positioning offset with vv.offsetTop subtracted, and —
  // critically — LOCK the body while it is open. Without the lock iOS
  // pans the layout viewport to "reveal" the input, which visually
  // launches every position:fixed element (this bubble included) off the
  // top of the screen. rAF-poll while our composer is focused because iOS
  // fires only sparse vv events mid-animation.
  useEffect(() => {
    const vv = window.visualViewport;
    if (!vv) return undefined;
    let rafId = null;
    let stopTimer = null;
    let locked = false;

    // While locked, only the bubble may consume touch scrolls — a drag on
    // the page behind would still pan the iOS visual viewport.
    const blockTouchOutside = (e) => {
      if (bubbleRef.current?.contains(e.target)) return;
      e.preventDefault();
    };
    const lock = () => {
      if (locked) return;
      locked = true;
      document.body.style.position = "fixed";
      document.body.style.width = "100%";
      document.body.style.top = "0";
      document.body.style.touchAction = "none";
      window.scrollTo(0, 0);
      document.addEventListener("touchmove", blockTouchOutside, { passive: false });
    };
    const unlock = () => {
      if (!locked) return;
      locked = false;
      document.body.style.position = "";
      document.body.style.width = "";
      document.body.style.top = "";
      document.body.style.touchAction = "";
      document.removeEventListener("touchmove", blockTouchOutside);
    };

    const update = () => {
      // rawDelta detects presence (ignores pan); offset positions us.
      const rawDelta = Math.max(0, Math.round(window.innerHeight - vv.height));
      const focusHere = bubbleRef.current?.contains(document.activeElement);
      const open = rawDelta > 100 && Boolean(focusHere);
      const offset = Math.max(
        0, Math.round(window.innerHeight - vv.height - vv.offsetTop),
      );
      const vvh = Math.round(vv.height);
      if (open) lock(); else unlock();
      setKb((prev) => (
        prev.open === open
          && Math.abs(prev.offset - offset) <= 3
          && Math.abs(prev.vvh - vvh) <= 3
          ? prev
          : { open, offset, vvh }
      ));
    };

    const poll = () => { update(); rafId = requestAnimationFrame(poll); };
    const startPoll = () => {
      if (stopTimer) { clearTimeout(stopTimer); stopTimer = null; }
      if (!rafId) rafId = requestAnimationFrame(poll);
    };
    const stopPoll = () => {
      // Delay the stop — keyboard-layout switches blur briefly.
      if (stopTimer) clearTimeout(stopTimer);
      stopTimer = setTimeout(() => {
        if (rafId) { cancelAnimationFrame(rafId); rafId = null; }
        update();
      }, 400);
    };

    vv.addEventListener("resize", update);
    vv.addEventListener("scroll", update);
    document.addEventListener("focusin", startPoll);
    document.addEventListener("focusout", stopPoll);
    update();
    return () => {
      vv.removeEventListener("resize", update);
      vv.removeEventListener("scroll", update);
      document.removeEventListener("focusin", startPoll);
      document.removeEventListener("focusout", stopPoll);
      if (rafId) cancelAnimationFrame(rafId);
      clearTimeout(stopTimer);
      unlock();
    };
  }, []);

  // ── Light dismissal: Esc + click-elsewhere ──
  useEffect(() => {
    const onKey = (e) => {
      if (e.key === "Escape") {
        e.stopPropagation();
        onClose?.();
      }
    };
    const onDown = (e) => {
      if (bubbleRef.current?.contains(e.target)) return;
      if (fabRef?.current?.contains(e.target)) return;
      onClose?.();
    };
    window.addEventListener("keydown", onKey, true);
    document.addEventListener("pointerdown", onDown, true);
    return () => {
      window.removeEventListener("keydown", onKey, true);
      document.removeEventListener("pointerdown", onDown, true);
    };
  }, [onClose, fabRef]);

  // ── Toast auto-dismiss (paused while hovered) ──
  const armToast = useCallback(() => {
    clearTimeout(toastTimer.current);
    toastTimer.current = setTimeout(() => onClose?.(), 9000);
  }, [onClose]);
  useEffect(() => {
    if (variant !== "toast") {
      clearTimeout(toastTimer.current);
      return undefined;
    }
    armToast();
    return () => clearTimeout(toastTimer.current);
  }, [variant, syncKey, armToast]);

  // ── Autofocus + keep scrolled to the newest message ──
  useEffect(() => {
    if (variant === "chat" && window.matchMedia?.("(pointer: fine)").matches) {
      inputRef.current?.focus();
    }
  }, [variant]);
  useEffect(() => {
    const el = scrollRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [msgs, busy, view, variant]);

  // Jobs load lazily — on first flip to the jobs view.
  const refreshJobs = useCallback(async () => {
    try {
      const r = await fetchAttentionJobs();
      setJobs(r.jobs || []);
    } catch {
      setJobs([]);
    }
  }, []);
  useEffect(() => {
    if (view === "jobs" && jobs === null) refreshJobs();
  }, [view, jobs, refreshJobs]);

  // ── Conversation ──
  const send = useCallback(async () => {
    const text = input.trim();
    if (!text || busy) return;
    setInput("");
    if (inputRef.current) inputRef.current.style.height = "auto";
    const afterUser = [...msgs, makeMsg("user", text)];
    commitMsgs(afterUser);
    setBusyBoth(true);
    try {
      const history = afterUser
        .slice(-HISTORY_SENT)
        .map((m) => ({ role: m.role, content: m.text }))
        .filter((m) => m.content);
      const r = await chatAttention(history);
      commitMsgs((prev) => [...prev, makeMsg("assistant", r.say, r.spec ? {
        spec: r.spec,
        specStatus: "pending",
      } : {})]);
    } catch (err) {
      // 422 detail is the assistant's own words (e.g. "say something
      // first") — show it as a bubble, not an error banner.
      commitMsgs((prev) => [...prev, makeMsg("assistant",
        err.message || "I could not reach the model — try again.",
        { error: true })]);
    } finally {
      setBusyBoth(false);
    }
  }, [input, busy, msgs, commitMsgs, setBusyBoth]);

  const confirmSpec = useCallback(async (msg) => {
    const spec = msg.spec;
    if (!spec || confirmingId) return;
    setConfirmingId(msg.id);
    try {
      const job = await createAttentionJob({
        kind: spec.kind,
        title: spec.title,
        source_text: spec.source_text,
        trigger_type: spec.trigger_type,
        trigger_config: spec.trigger_config,
        action_type: spec.action_type,
        action_config: spec.action_config,
      });
      commitMsgs((prev) => prev.map((m) => (m.id === msg.id
        ? { ...m, specStatus: "created", createdRunAt: job.next_run_at }
        : m)));
      setJobs(null);  // stale — refetch next time the pill is opened
      onCelebrate?.();
    } catch (err) {
      commitMsgs((prev) => [...prev, makeMsg("assistant",
        err.message || "That failed to save.", { error: true })]);
    } finally {
      setConfirmingId(null);
    }
  }, [confirmingId, commitMsgs, onCelebrate]);

  const cancelSpec = useCallback((msg) => {
    commitMsgs((prev) => prev.map((m) => (m.id === msg.id
      ? { ...m, specStatus: "cancelled" }
      : m)));
  }, [commitMsgs]);

  // Preset chips — the instant, model-free path, kept from the old panel.
  const handlePreset = useCallback(async (preset) => {
    const text = input.trim();
    if (!text || busy) return;
    setInput("");
    const afterUser = [...msgs, makeMsg("user", text)];
    commitMsgs(afterUser);
    try {
      const job = await createAttentionJob(presetSpec(preset, text));
      const when = job.next_run_at ? ` — I'll ping you ${relativeTime(job.next_run_at)}` : "";
      commitMsgs((prev) => [...prev, makeMsg("assistant", `Got it${when}.`, {
        specStatus: "created",
        createdRunAt: job.next_run_at,
      })]);
      setJobs(null);
      onCelebrate?.();
    } catch (err) {
      commitMsgs((prev) => [...prev, makeMsg("assistant",
        err.message || "That failed to save.", { error: true })]);
    }
  }, [input, busy, msgs, commitMsgs, onCelebrate]);

  const mutateJob = useCallback(async (fn) => {
    try {
      await fn();
    } catch {
      /* row actions are retryable; the refreshed list shows the truth */
    }
    await refreshJobs();
  }, [refreshJobs]);

  const pickCharacter = useCallback((c) => {
    setCharacter(c);
    setActiveChar(c);
  }, []);

  const generateCharacter = useCallback(async () => {
    const t = charPrompt.trim();
    if (!t || charBusy) return;
    setCharBusy(true);
    setCharError(null);
    setCharPreview(null);
    try {
      const r = await generateAttentionCharacter(t);
      setCharPreview(r.character);
    } catch (err) {
      setCharError(err.message || "Generation failed — try again.");
    } finally {
      setCharBusy(false);
    }
  }, [charPrompt, charBusy]);

  const adoptPreview = useCallback(() => {
    if (!charPreview) return;
    saveGeneratedCharacter(charPreview);
    setSavedChars(listSavedCharacters());
    setCharacter(charPreview);
    setActiveChar(charPreview);
    setCharPreview(null);
    setCharPrompt("");
    onCelebrate?.();
  }, [charPreview, onCelebrate]);

  const removeSaved = useCallback((id) => {
    deleteSavedCharacter(id);
    setSavedChars(listSavedCharacters());
    if (activeChar?.id === id) {
      setCharacter(null);
      setActiveChar(DEFAULT_CHARACTER);
    }
  }, [activeChar]);

  const onKeyDown = (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      send();
    }
  };

  const autoGrow = (e) => {
    const el = e.target;
    el.style.height = "auto";
    el.style.height = `${Math.min(el.scrollHeight, designPx(92))}px`;
  };

  const openUrl = useCallback((url) => {
    navigate(url);
    onClose?.();
  }, [navigate, onClose]);

  if (!anchor) return null;
  // layoutTick re-runs this on resize; kb rides on top for the keyboard.
  void layoutTick;
  const L = computeLayout(anchor, variant);

  // With the body locked, the keyboard simply overlaps the bottom
  // `kb.offset` px of the layout viewport: shift up only as far as needed
  // to clear it, and never grow taller than what stays visible.
  let shiftY = 0;
  let maxH = L.maxHeight;
  if (kb.open) {
    if (L.above) {
      const clearance = kb.offset + 8;
      if (clearance > L.bottom) shiftY = clearance - L.bottom;
      maxH = Math.min(L.maxHeight, kb.vvh - 16);
    } else {
      maxH = Math.min(L.maxHeight, window.innerHeight - kb.offset - L.top - 8);
    }
    maxH = Math.max(200, maxH);
  }

  const posStyle = {
    left: L.left,
    width: L.width,
    ...(L.above ? { bottom: L.bottom } : { top: L.top }),
    transform: shiftY ? `translateY(-${shiftY}px)` : undefined,
    transformOrigin: `${L.tailX}px ${L.above ? "100%" : "0%"}`,
  };

  const tail = (
    <div
      aria-hidden="true"
      className="absolute w-3.5 h-3.5 rotate-45 bg-page"
      style={{
        left: L.tailX - 7,
        ...(L.above
          ? {
            bottom: -7,
            borderRight: "1px solid var(--color-edge)",
            borderBottom: "1px solid var(--color-edge)",
          }
          : {
            top: -7,
            borderLeft: "1px solid var(--color-edge)",
            borderTop: "1px solid var(--color-edge)",
          }),
      }}
    />
  );

  // ── Toast: the ball says one thing, then stops ──
  if (variant === "toast") {
    const last = [...msgs].reverse().find((m) => m.fired) || msgs[msgs.length - 1];
    return createPortal(
      <div
        ref={bubbleRef}
        role="status"
        className="fixed z-[55] attn-bubble-pop rounded-2xl bg-page border border-edge shadow-xl"
        style={posStyle}
        onPointerEnter={() => clearTimeout(toastTimer.current)}
        onPointerLeave={armToast}
      >
        {tail}
        <div className="px-3 pt-2 pb-2">
          {last?.title && (
            <p className="text-[12px] font-semibold text-heading leading-snug">
              {last.title}
            </p>
          )}
          <p className="text-[12.5px] text-body leading-snug break-words">
            {last?.text}
          </p>
          <div className="mt-1.5 flex items-center gap-3">
            {last?.url && (
              <button
                type="button"
                onClick={() => openUrl(last.url)}
                className="text-[11px] font-semibold text-attn hover:opacity-80"
              >
                Open →
              </button>
            )}
            <button
              type="button"
              onClick={onExpand}
              className="text-[11px] font-medium text-dim hover:text-heading"
            >
              Reply…
            </button>
            <button
              type="button"
              aria-label="Dismiss"
              onClick={onClose}
              className="ml-auto -mr-1 p-1 rounded text-faint hover:text-heading"
            >
              <svg className="w-3 h-3" fill="none" stroke="currentColor" strokeWidth={2.2} viewBox="0 0 24 24">
                <path strokeLinecap="round" d="M6 18L18 6M6 6l12 12" />
              </svg>
            </button>
          </div>
        </div>
      </div>,
      document.body,
    );
  }

  // ── Full chat ──
  const activeCount = jobs === null
    ? jobCount
    : jobs.filter((j) => j.status === "active").length;

  return createPortal(
    <div
      ref={bubbleRef}
      role="dialog"
      aria-label="Assistant"
      className="fixed z-[55] attn-bubble-pop flex flex-col rounded-2xl bg-page border border-edge shadow-xl"
      style={{ ...posStyle, maxHeight: maxH }}
    >
      {tail}

      {/* ── Slim header ── */}
      <div className="shrink-0 flex items-center gap-1.5 pl-3 pr-1.5 pt-2 pb-1">
        <span className="text-[11px] font-semibold text-heading">Assistant</span>
        {unreadList.length > 0 && (
          <button
            type="button"
            onClick={() => { onOpenAgent?.(unreadList[0].id); onClose?.(); }}
            className="px-1.5 py-px rounded-full bg-attn text-attn-ink text-[10px] font-semibold hover:opacity-90"
          >
            {unreadList.length} waiting
          </button>
        )}
        <div className="ml-auto flex items-center gap-0.5">
          <button
            type="button"
            onClick={() => setView((v) => (v === "face" ? "chat" : "face"))}
            aria-pressed={view === "face"}
            aria-label="Choose assistant face"
            title="Choose assistant face"
            className={`p-1 rounded-lg transition-colors ${view === "face" ? "attn-tint-12" : "hover:bg-hover"}`}
          >
            <AttentionOrb
              mood="done"
              character={activeChar?.palette ? activeChar : null}
              className="w-[18px] h-[18px]"
            />
          </button>
          <button
            type="button"
            onClick={() => setView((v) => (v === "jobs" ? "chat" : "jobs"))}
            aria-pressed={view === "jobs"}
            className={`px-2 py-0.5 rounded-full text-[10px] font-semibold transition-colors ${
              view === "jobs"
                ? "bg-attn text-attn-ink"
                : "attn-tint-12 text-attn attn-tint-hover"
            }`}
          >
            {activeCount > 0 ? `${activeCount} job${activeCount === 1 ? "" : "s"}` : "jobs"}
          </button>
          <button
            type="button"
            aria-label="Close assistant"
            onClick={onClose}
            className="p-1.5 rounded-lg text-faint hover:text-heading hover:bg-hover transition-colors"
          >
            <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" strokeWidth={2.2} viewBox="0 0 24 24">
              <path strokeLinecap="round" d="M6 18L18 6M6 6l12 12" />
            </svg>
          </button>
        </div>
      </div>

      {view === "face" ? (
        <div className="flex-1 min-h-[140px] overflow-y-auto px-3 pb-3 pt-1 overscroll-contain">
          <p className="text-[10px] font-semibold text-label uppercase tracking-wide mb-1.5">
            Choose a face
          </p>
          <div className="grid grid-cols-4 gap-1.5">
            {[...PRESET_CHARACTERS, ...savedChars].map((c) => (
              <button
                key={c.id}
                type="button"
                onClick={() => pickCharacter(c)}
                className={`relative flex flex-col items-center gap-0.5 px-1 pt-1.5 pb-1 rounded-xl border transition-colors ${
                  activeChar?.id === c.id
                    ? "attn-edge-40 attn-tint-8"
                    : "border-transparent hover:bg-hover"
                }`}
              >
                <AttentionOrb mood="done" character={c.palette ? c : null} className="w-9 h-9" />
                <span className="text-[9.5px] text-dim truncate max-w-full">{c.name}</span>
                {savedChars.some((sc) => sc.id === c.id) && (
                  <span
                    role="button"
                    aria-label={`Delete ${c.name}`}
                    onClick={(e) => { e.stopPropagation(); removeSaved(c.id); }}
                    className="absolute -top-1 -right-1 w-4 h-4 rounded-full bg-elevated text-faint text-[9px] leading-4 text-center hover:text-heading"
                  >
                    ✕
                  </span>
                )}
              </button>
            ))}
          </div>

          <p className="mt-3 text-[10px] font-semibold text-label uppercase tracking-wide mb-1.5">
            Design your own
          </p>
          {charPreview ? (
            <div className="rounded-xl border attn-edge-25 attn-tint-6 px-3 py-2.5 flex items-center gap-3">
              <AttentionOrb mood="done" character={charPreview} className="w-12 h-12 shrink-0" />
              <div className="min-w-0 flex-1">
                <p className="text-[12px] font-medium text-heading truncate">{charPreview.name}</p>
                <div className="mt-1.5 flex items-center gap-1.5">
                  <button
                    type="button"
                    onClick={adoptPreview}
                    className="px-2.5 py-0.5 rounded-full text-[11px] font-semibold bg-attn text-attn-ink hover:opacity-90"
                  >
                    Use it
                  </button>
                  <button
                    type="button"
                    onClick={() => setCharPreview(null)}
                    className="px-2 py-0.5 rounded-full text-[11px] font-medium text-dim hover:text-heading hover:bg-hover"
                  >
                    Discard
                  </button>
                </div>
              </div>
            </div>
          ) : (
            <div className="flex items-center gap-1.5">
              <input
                value={charPrompt}
                onChange={(e) => setCharPrompt(e.target.value)}
                onKeyDown={(e) => { if (e.key === "Enter") generateCharacter(); }}
                disabled={charBusy}
                placeholder="a shiba inu, 一只熊猫, a little robot…"
                className="flex-1 min-w-0 px-2.5 py-1.5 text-[12px] rounded-xl bg-input border border-edge text-body placeholder:text-faint outline-none focus:border-attn transition-colors disabled:opacity-60"
              />
              <button
                type="button"
                disabled={charBusy || !charPrompt.trim()}
                onClick={generateCharacter}
                className="shrink-0 px-2.5 py-1.5 rounded-xl text-[11px] font-semibold bg-attn text-attn-ink hover:opacity-90 disabled:opacity-40 transition-opacity"
              >
                {charBusy ? "Designing…" : "Generate"}
              </button>
            </div>
          )}
          {charBusy && (
            <p className="mt-2 flex items-center gap-2">
              <AttentionOrb mood="thinking" className="w-6 h-6 shrink-0" />
              <span className="text-[11px] text-dim">
                The designer model is drawing — usually 30–60s.
              </span>
            </p>
          )}
          {charError && (
            <p className="mt-1.5 text-[10.5px] text-red-500 dark:text-red-400 leading-snug">
              {charError}
            </p>
          )}
        </div>
      ) : view === "jobs" ? (
        <div className="flex-1 min-h-[120px] overflow-y-auto px-2.5 pb-2.5 pt-1 space-y-1.5 overscroll-contain">
          {jobs === null ? (
            <p className="py-6 text-center text-[12px] text-dim animate-pulse">Loading…</p>
          ) : jobs.length === 0 ? (
            <p className="py-6 text-center text-[12px] text-dim">
              Nothing pending — ask me below.
            </p>
          ) : (
            jobs.map((job) => (
              <JobRow
                key={job.id}
                job={job}
                onSnooze={() => mutateJob(() => snoozeAttentionJob(job.id, 15))}
                onToggle={() => mutateJob(() => patchAttentionJob(job.id, {
                  status: job.status === "paused" ? "active" : "paused",
                }))}
                onDelete={() => mutateJob(() => deleteAttentionJob(job.id))}
                onRunNow={() => mutateJob(() => runAttentionJobNow(job.id))}
              />
            ))
          )}
        </div>
      ) : (
        <>
          {/* ── Transcript ── */}
          <div
            ref={scrollRef}
            className="flex-1 min-h-[100px] overflow-y-auto px-2.5 py-1.5 space-y-1.5 overscroll-contain"
          >
            {msgs.length === 0 && (
              <div className="attn-msg-in space-y-1.5">
                <Assistant>
                  <p>Hi! Ask me in plain words — reminders, agent watches, scheduled digests.</p>
                </Assistant>
                <div className="flex flex-wrap gap-1 pl-1">
                  {STARTERS.map((s) => (
                    <button
                      key={s}
                      type="button"
                      onClick={() => { setInput(s); inputRef.current?.focus(); }}
                      className="px-2 py-0.5 rounded-full text-[10.5px] attn-tint-8 text-attn attn-tint-hover transition-colors text-left"
                    >
                      {s}
                    </button>
                  ))}
                </div>
              </div>
            )}

            {msgs.map((m) => (m.role === "user" ? (
              <div key={m.id} className="flex justify-end attn-msg-in">
                <div className="max-w-[85%] px-2.5 py-1.5 rounded-2xl rounded-br-md bg-attn text-attn-ink text-[12.5px] leading-snug break-words whitespace-pre-wrap">
                  {m.text}
                </div>
              </div>
            ) : (
              <div key={m.id} className="attn-msg-in">
                <Assistant error={m.error}>
                  {m.fired && m.title && (
                    <p className="font-semibold text-heading">{m.title}</p>
                  )}
                  <p className="whitespace-pre-wrap">{m.text}</p>

                  {m.fired && m.url && (
                    <button
                      type="button"
                      onClick={() => openUrl(m.url)}
                      className="mt-1 text-[11px] font-semibold text-attn hover:opacity-80"
                    >
                      Open →
                    </button>
                  )}

                  {/* Inline proposal — the confirm step, as chat. */}
                  {m.spec && m.specStatus === "pending" && (
                    <div className="mt-1.5 pt-1.5 border-t attn-edge-25">
                      {m.spec.preview_next_run_at && (
                        <p className="text-[10.5px] text-dim mb-1.5">
                          {/* Trigger arithmetic, not model prose — the user
                              always confirms the real schedule. */}
                          First run {firstRunLine(m.spec.preview_next_run_at)}
                        </p>
                      )}
                      <div className="flex items-center gap-1.5">
                        <button
                          type="button"
                          disabled={confirmingId === m.id}
                          onClick={() => confirmSpec(m)}
                          className="px-2.5 py-0.5 rounded-full text-[11px] font-semibold bg-attn text-attn-ink hover:opacity-90 disabled:opacity-40 transition-opacity"
                        >
                          {confirmingId === m.id ? "Saving…" : "Create"}
                        </button>
                        <button
                          type="button"
                          onClick={() => cancelSpec(m)}
                          className="px-2 py-0.5 rounded-full text-[11px] font-medium text-dim hover:text-heading hover:bg-hover transition-colors"
                        >
                          Cancel
                        </button>
                        {m.spec.costly && (
                          <span className="ml-auto text-[9.5px] text-dim">spends tokens</span>
                        )}
                      </div>
                    </div>
                  )}
                  {m.specStatus === "created" && (
                    <p className="mt-1 text-[10.5px] font-medium text-attn">
                      ✓ Created{m.createdRunAt ? ` · first run ${relativeTime(m.createdRunAt)}` : ""}
                    </p>
                  )}
                  {m.specStatus === "cancelled" && (
                    <p className="mt-1 text-[10.5px] text-faint">Dismissed</p>
                  )}
                </Assistant>
              </div>
            )))}

            {busy && (
              <div className="attn-msg-in">
                <Assistant>
                  <span className="inline-flex items-center gap-1 py-0.5" aria-label="Assistant is thinking">
                    <span className="attn-dot w-1.5 h-1.5 rounded-full bg-attn inline-block" />
                    <span className="attn-dot attn-dot-b w-1.5 h-1.5 rounded-full bg-attn inline-block" />
                    <span className="attn-dot attn-dot-c w-1.5 h-1.5 rounded-full bg-attn inline-block" />
                  </span>
                </Assistant>
              </div>
            )}
          </div>

          {/* ── Presets: instant, model-free — only once there is text ── */}
          {input.trim() && !busy && (
            <div className="shrink-0 flex flex-wrap items-center gap-1 px-2.5 pb-1">
              {PRESETS.map((p) => (
                <button
                  key={p.label}
                  type="button"
                  onClick={() => handlePreset(p)}
                  className="px-2 py-0.5 rounded-full text-[10.5px] font-medium attn-tint-12 text-attn attn-tint-hover transition-colors"
                >
                  {p.label}
                </button>
              ))}
            </div>
          )}

          {/* ── Composer ── */}
          <div className="shrink-0 flex items-end gap-1.5 px-2.5 pb-2.5 pt-1">
            <textarea
              ref={inputRef}
              rows={1}
              value={input}
              onChange={(e) => setInput(e.target.value)}
              onInput={autoGrow}
              onKeyDown={onKeyDown}
              placeholder="Ask me anything…"
              enterKeyHint="send"
              className="flex-1 resize-none px-3 py-1.5 text-[13px] leading-snug rounded-2xl bg-input border border-edge text-body placeholder:text-faint outline-none focus:border-attn transition-colors"
            />
            <button
              type="button"
              aria-label="Send"
              disabled={busy || !input.trim()}
              onClick={send}
              className="shrink-0 w-8 h-8 rounded-full bg-attn text-attn-ink flex items-center justify-center hover:opacity-90 disabled:opacity-40 transition-opacity"
            >
              <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" strokeWidth={2.2} viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" d="M5 12h13M12 5l7 7-7 7" />
              </svg>
            </button>
          </div>
        </>
      )}
    </div>,
    document.body,
  );
}

function Assistant({ children, error = false }) {
  return (
    <div className="flex justify-start">
      <div
        className={`max-w-[88%] px-2.5 py-1.5 rounded-2xl rounded-bl-md text-[12.5px] leading-snug break-words ${
          error
            ? "bg-red-500/10 text-danger"
            : "attn-tint-8 text-body"
        }`}
      >
        {children}
      </div>
    </div>
  );
}
