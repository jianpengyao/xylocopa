import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  TEXT_SCALE_EVENT, TEXT_SCALE_MAX, TEXT_SCALE_MIN, TEXT_SCALE_STEP,
  clampTextScale, effectiveTextScale, getMaxTextScale, getTextScale, setTextScale,
} from "../lib/textScale";

// Settings > Text size: per-device UI scale (see lib/textScale.js).
//
// The slider applies on release, not while dragging: each step reflows the
// whole page, and where this card's width is rem-based (wide screens) a live
// drag would resize the track under the finger and fight the thumb. The A
// buttons apply at once. Every apply keeps this card at the same spot on
// screen, so repeated taps land on the same button even though the content
// above it grows or shrinks.

const pct = (v) => `${Math.round(v * 100)}%`;

function scrollParentOf(el) {
  for (let p = el?.parentElement; p; p = p.parentElement) {
    const oy = getComputedStyle(p).overflowY;
    if ((oy === "auto" || oy === "scroll") && p.scrollHeight > p.clientHeight) return p;
  }
  return document.scrollingElement;
}

function StepButton({ label, onClick, disabled, children }) {
  return (
    <button
      type="button"
      aria-label={label}
      title={label}
      onClick={onClick}
      disabled={disabled}
      className="w-9 h-9 shrink-0 rounded-lg bg-input text-heading hover:bg-hover disabled:opacity-40 disabled:cursor-not-allowed flex items-center justify-center transition-colors"
    >
      {children}
    </button>
  );
}

export default function TextSizeSettings() {
  const sectionRef = useRef(null);
  const sliderRef = useRef(null);
  const maxScale = useMemo(() => getMaxTextScale(), []);
  const [chosen, setChosen] = useState(() => getTextScale());
  const [effective, setEffective] = useState(() => effectiveTextScale(getTextScale()));
  // Slider value while a drag is in progress; null otherwise.
  const [draft, setDraft] = useState(null);

  // Follow changes made elsewhere: window cap on resize, another tab.
  useEffect(() => {
    const sync = () => {
      const c = getTextScale();
      setChosen(c);
      setEffective(effectiveTextScale(c));
    };
    window.addEventListener(TEXT_SCALE_EVENT, sync);
    return () => window.removeEventListener(TEXT_SCALE_EVENT, sync);
  }, []);

  const apply = useCallback((value) => {
    setDraft(null);
    const next = Math.min(clampTextScale(value), maxScale);
    if (next === getTextScale()) return;
    const el = sectionRef.current;
    const before = el ? el.getBoundingClientRect().top : null;
    setTextScale(next);
    // Reading the rect forces the reflow at the new size.
    if (el && before != null) {
      const delta = el.getBoundingClientRect().top - before;
      const scroller = scrollParentOf(el);
      if (delta && scroller) scroller.scrollTop += delta;
    }
  }, [maxScale]);

  // Native `change` fires once the drag is released (and per key press);
  // React's onChange fires on every input event, which only moves the draft.
  // pointerup/touchend commit too, in case an engine skips `change` after a
  // touch drag; a repeated commit of the same value is a no-op.
  useEffect(() => {
    const slider = sliderRef.current;
    if (!slider) return undefined;
    const commit = () => apply(parseFloat(slider.value));
    const events = ["change", "pointerup", "touchend"];
    events.forEach((ev) => slider.addEventListener(ev, commit));
    return () => events.forEach((ev) => slider.removeEventListener(ev, commit));
  }, [apply]);

  const shown = draft ?? chosen;
  const narrowed = effective < chosen - 0.001;

  return (
    <section ref={sectionRef} className="rounded-xl bg-surface shadow-card p-4">
      <div className="flex items-start justify-between gap-4">
        <div className="min-w-0">
          <h3 className="text-sm font-medium text-heading">Text size</h3>
          <p className="text-xs text-dim mt-1 leading-relaxed">
            Scales all text together with spacing and icons, and the layout
            reflows to fit the screen. Saved on this device only.
          </p>
        </div>
        <button
          type="button"
          onClick={() => apply(1)}
          disabled={chosen === 1}
          className="text-xs text-accent hover:underline shrink-0 disabled:opacity-40 disabled:no-underline disabled:cursor-default"
        >
          Reset
        </button>
      </div>

      <div className="mt-3 flex items-center gap-2">
        <StepButton
          label="Smaller text"
          onClick={() => apply(chosen - TEXT_SCALE_STEP)}
          disabled={chosen <= TEXT_SCALE_MIN}
        >
          <span className="text-xs font-semibold leading-none">A</span>
        </StepButton>
        <input
          ref={sliderRef}
          type="range"
          min={TEXT_SCALE_MIN}
          max={maxScale}
          step={TEXT_SCALE_STEP}
          value={Math.min(shown, maxScale)}
          onChange={(e) => setDraft(parseFloat(e.target.value))}
          aria-label="Text size"
          aria-valuetext={pct(shown)}
          className="flex-1 min-w-0 h-9 form-accent cursor-pointer"
        />
        <StepButton
          label="Larger text"
          onClick={() => apply(chosen + TEXT_SCALE_STEP)}
          disabled={chosen >= maxScale}
        >
          <span className="text-lg font-semibold leading-none">A</span>
        </StepButton>
        <span className="w-11 shrink-0 text-right text-sm font-medium text-heading tabular-nums">
          {pct(shown)}
        </span>
      </div>

      {(maxScale < TEXT_SCALE_MAX || narrowed) && (
        <p className="text-[11px] text-faint mt-2 leading-relaxed">
          {narrowed
            ? `Showing ${pct(effective)}: this window is too narrow for ${pct(chosen)}.`
            : `Up to ${pct(maxScale)} on this screen, so the layout still fits.`}
        </p>
      )}
    </section>
  );
}
