import { useState, useRef, useEffect, useCallback } from "react";
import { createPortal } from "react-dom";
import { designPx } from "../lib/textScale";

// Draggable vertical stack of minimized web-app chips. Drag behavior adapted
// from DraggableFab, but for a container with interactive children: taps fall
// through to the chips natively; once movement passes the threshold the
// trailing click is suppressed so a drag never triggers restore/close.

const DRAG_THRESHOLD = 8;

// Position stored as {right, bottom} — offset from viewport edges, so the
// dock keeps its place on resize and grows upward as chips are added.
function toAbs(rb, w, h) {
  return { x: window.innerWidth - rb.right - w, y: window.innerHeight - rb.bottom - h };
}

function toRB(abs, w, h) {
  return { right: window.innerWidth - abs.x - w, bottom: window.innerHeight - abs.y - h };
}

function clampRB(rb, w, h) {
  return {
    right: Math.max(0, Math.min(rb.right, window.innerWidth - w)),
    bottom: Math.max(0, Math.min(rb.bottom, window.innerHeight - h)),
  };
}

const STORAGE_KEY = "ah:webapp-dock-pos-v1";

// Default: left edge, above the prompt input bar (matches the old fixed row).
const defaultRB = (w) => ({
  right: window.innerWidth - 12 - w,
  bottom: designPx(96),
});

export default function WebAppDock({ chips, onRestore, onClose }) {
  const dockRef = useRef(null);
  const sizeRef = useRef({ w: 140, h: 36 });
  const [rb, setRB] = useState(null);
  const rbRef = useRef(rb);
  rbRef.current = rb;
  const dragging = useRef(false);
  const dragStart = useRef({ x: 0, y: 0 });
  const moved = useRef(false);

  // Resolve position on mount
  useEffect(() => {
    if (dockRef.current) {
      const rect = dockRef.current.getBoundingClientRect();
      sizeRef.current = { w: rect.width, h: rect.height };
    }
    try {
      const saved = localStorage.getItem(STORAGE_KEY);
      if (saved) {
        const p = JSON.parse(saved);
        if (p.right != null && p.bottom != null) {
          setRB(clampRB(p, sizeRef.current.w, sizeRef.current.h));
          return;
        }
      }
    } catch { /* use default */ }
    setRB(defaultRB(sizeRef.current.w));
  }, []);

  // Re-measure and clamp whenever the chip set changes — the stack grows
  // upward from its bottom anchor and must not slide off-screen.
  useEffect(() => {
    if (!dockRef.current) return;
    const rect = dockRef.current.getBoundingClientRect();
    sizeRef.current = { w: rect.width, h: rect.height };
    setRB((cur) => (cur ? clampRB(cur, rect.width, rect.height) : cur));
  }, [chips.length]);

  const onStart = useCallback((e) => {
    if (e.type === "mousedown" && e.button !== 0) return;
    const t = e.touches ? e.touches[0] : e;
    dragStart.current = { x: t.clientX, y: t.clientY };
    moved.current = false;
    dragging.current = true;
    // No preventDefault here — children must still receive native clicks.
  }, []);

  useEffect(() => {
    const onMove = (e) => {
      if (!dragging.current) return;
      const t = e.touches ? e.touches[0] : e;
      const dx = t.clientX - dragStart.current.x;
      const dy = t.clientY - dragStart.current.y;
      if (!moved.current && Math.abs(dx) + Math.abs(dy) < DRAG_THRESHOLD) return;
      if (!moved.current && dockRef.current) dockRef.current.style.transition = "none";
      moved.current = true;
      if (e.cancelable) e.preventDefault(); // stop page scroll on touch
      // Direct DOM manipulation — no React re-render during drag
      if (dockRef.current) {
        dockRef.current.style.transform = `translate3d(${dx}px, ${dy}px, 0)`;
      }
    };

    const onEnd = () => {
      if (!dragging.current) return;
      dragging.current = false;
      if (!moved.current) return; // tap — let the chip's own click handler run
      const el = dockRef.current;
      if (!el) return;
      const rect = el.getBoundingClientRect();
      el.style.transform = "";
      sizeRef.current = { w: rect.width, h: rect.height };
      const final_ = clampRB(toRB({ x: rect.left, y: rect.top }, rect.width, rect.height), rect.width, rect.height);
      requestAnimationFrame(() => { if (el) el.style.transition = ""; });
      setRB(final_);
      try { localStorage.setItem(STORAGE_KEY, JSON.stringify(final_)); } catch { /* ok */ }
    };

    window.addEventListener("mousemove", onMove);
    window.addEventListener("mouseup", onEnd);
    window.addEventListener("touchmove", onMove, { passive: false });
    window.addEventListener("touchend", onEnd);
    return () => {
      window.removeEventListener("mousemove", onMove);
      window.removeEventListener("mouseup", onEnd);
      window.removeEventListener("touchmove", onMove);
      window.removeEventListener("touchend", onEnd);
    };
  }, []);

  // After a drag, swallow the click the browser fires on the chip under the
  // pointer so dropping the dock never restores/closes an app.
  const onClickCapture = useCallback((e) => {
    if (!moved.current) return;
    moved.current = false;
    e.preventDefault();
    e.stopPropagation();
  }, []);

  if (chips.length === 0) return null;

  return createPortal(
    <div
      ref={dockRef}
      onMouseDown={onStart}
      onTouchStart={onStart}
      onClickCapture={onClickCapture}
      className="fixed z-40 flex flex-col items-end gap-1 select-none cursor-grab active:cursor-grabbing"
      style={{
        right: rb ? rb.right : 12,
        bottom: rb ? rb.bottom : 96,
        visibility: rb ? "visible" : "hidden",
        touchAction: "none",
        willChange: "transform",
      }}
    >
      {chips.map(({ key, app }) => (
        <div
          key={key}
          onClick={() => onRestore(key)}
          title={`Restore ${app.filename}`}
          className="flex items-center gap-1 pl-2 pr-1 py-1 rounded-full bg-elevated border border-divider shadow-card cursor-pointer hover:bg-hover transition-colors"
        >
          <svg className="w-3 h-3 text-accent shrink-0" fill="none" stroke="currentColor" strokeWidth={1.5} viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" d="M12 21a9 9 0 100-18 9 9 0 000 18zm0 0c2.485 0 4.5-4.03 4.5-9S14.485 3 12 3 7.5 7.03 7.5 12s2.015 9 4.5 9zM3.6 9h16.8M3.6 15h16.8" />
          </svg>
          <span className="text-[11px] leading-4 text-label max-w-[96px] truncate">{app.filename}</span>
          <button
            type="button"
            onClick={(e) => { e.stopPropagation(); onClose(key); }}
            title="Close app"
            className="p-0.5 rounded-full hover:bg-hover text-dim hover:text-label"
          >
            <svg className="w-3 h-3" fill="none" stroke="currentColor" strokeWidth={2} viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" d="M6 18L18 6M6 6l12 12" />
            </svg>
          </button>
        </div>
      ))}
    </div>,
    document.body
  );
}
