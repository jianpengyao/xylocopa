import { useState, useEffect, useLayoutEffect, useRef, useMemo } from "react";
import { createPortal } from "react-dom";
import FluentEmoji from "./FluentEmoji";
import { designPx, rem } from "../lib/textScale";
import { CATEGORIES, KEYWORDS, ALL_EMOJIS } from "../lib/projectEmoji";

const PICKER_W = 296;
const PICKER_H = 420;

/**
 * Emoji picker portal, styled to mirror SendLaterPicker.
 *
 * Props:
 *   current   — currently selected emoji char (or null)
 *   onSelect  — (char) => void; called on emoji click
 *   onClear   — () => void; called on "Reset" click (clears to default)
 *   onClose   — () => void; called on outside click / ✕ / after select
 *   anchorRect — optional DOMRect to position against (falls back to invisible span)
 */
export default function EmojiPicker({ current, onSelect, onClear, onClose, anchorRect }) {
  const anchorRef = useRef(null);
  const pickerRef = useRef(null);
  const [pos, setPos] = useState(null);
  const [category, setCategory] = useState(CATEGORIES[0].key);
  const [query, setQuery] = useState("");

  useLayoutEffect(() => {
    const rect = anchorRect || anchorRef.current?.getBoundingClientRect();
    if (!rect) return;
    // Rendered size: the picker is sized in rem (Settings > Text size).
    const pw = designPx(PICKER_W);
    const ph = designPx(PICKER_H);
    let right = window.innerWidth - rect.right;
    if (rect.right - pw < 8) right = window.innerWidth - pw - 8;
    if (right < 8) right = 8;
    const spaceBelow = window.innerHeight - rect.bottom;
    if (spaceBelow >= ph + 8) {
      setPos({ top: rect.bottom + 6, right });
    } else if (rect.top >= ph + 8) {
      setPos({ bottom: window.innerHeight - rect.top + 6, right });
    } else {
      setPos({ top: Math.max(8, window.innerHeight - ph - 8), right });
    }
  }, [anchorRect]);

  useEffect(() => {
    const handler = (e) => {
      if (pickerRef.current && !pickerRef.current.contains(e.target)) onClose();
    };
    document.addEventListener("mousedown", handler);
    document.addEventListener("touchstart", handler);
    return () => {
      document.removeEventListener("mousedown", handler);
      document.removeEventListener("touchstart", handler);
    };
  }, [onClose]);

  const trimmed = query.trim().toLowerCase();
  const filtered = useMemo(() => {
    if (!trimmed) return null;
    return ALL_EMOJIS.filter((e) => (KEYWORDS[e] || "").includes(trimmed));
  }, [trimmed]);

  const current_cat = CATEGORIES.find(c => c.key === category) || CATEGORIES[0];
  const emojis = filtered != null ? filtered : current_cat.emojis;

  const handlePick = (char) => {
    onSelect(char);
    onClose();
  };

  const handleReset = () => {
    onClear?.();
    onClose();
  };

  const posStyle = pos ? { position: "fixed", ...pos } : { visibility: "hidden", position: "fixed" };

  const picker = (
    <div
      ref={pickerRef}
      data-card
      className="bg-surface border border-divider rounded-2xl shadow-xl overflow-hidden z-[9999] flex flex-col"
      style={{ ...posStyle, width: rem(PICKER_W), maxHeight: rem(PICKER_H) }}
    >
      {/* Header */}
      <div className="px-4 py-2.5 flex items-center justify-between border-b border-divider shrink-0">
        <span className="text-sm font-semibold text-heading">Project Icon</span>
        <div className="flex items-center gap-2">
          {onClear && (
            <button type="button" onClick={handleReset}
              className="text-xs text-faint hover:text-dim font-medium transition-colors">
              Reset
            </button>
          )}
          <button type="button" onClick={onClose}
            className="w-6 h-6 rounded-full bg-elevated flex items-center justify-center text-dim hover:text-heading transition-colors">
            <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" strokeWidth={2.5} viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" d="M6 18L18 6M6 6l12 12" />
            </svg>
          </button>
        </div>
      </div>

      {/* Search */}
      <div className="px-3 pt-2.5 pb-2 shrink-0">
        <div className="relative">
          <svg className="absolute left-2.5 top-1/2 -translate-y-1/2 w-3.5 h-3.5 text-faint pointer-events-none"
            fill="none" stroke="currentColor" strokeWidth={2} viewBox="0 0 24 24">
            <circle cx="11" cy="11" r="8" />
            <path strokeLinecap="round" d="m21 21-4.35-4.35" />
          </svg>
          <input
            type="text"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="Search..."
            autoFocus
            className="w-full h-8 pl-8 pr-7 rounded-lg bg-elevated text-sm text-body placeholder-hint outline-none focus:ring-1 focus:ring-accent"
          />
          {query && (
            <button type="button" onClick={() => setQuery("")}
              className="absolute right-2 top-1/2 -translate-y-1/2 text-faint hover:text-label">
              <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" strokeWidth={2} viewBox="0 0 24 24">
                <path strokeLinecap="round" d="M6 18 18 6M6 6l12 12" />
              </svg>
            </button>
          )}
        </div>
      </div>

      {/* Category chips — muted toolbar band, hidden while searching */}
      {!trimmed && (
        <div className="px-2 pb-2 shrink-0">
          <div className="flex items-center gap-0.5 overflow-x-auto no-scrollbar bg-elevated rounded-xl p-1">
            {CATEGORIES.map((cat) => {
              const isActive = cat.key === category;
              return (
                <button
                  key={cat.key}
                  type="button"
                  onClick={() => setCategory(cat.key)}
                  title={cat.label}
                  className={`shrink-0 w-7 h-7 rounded-lg flex items-center justify-center transition-colors ${
                    isActive ? "bg-surface shadow-sm" : "hover:bg-surface/50"
                  }`}
                >
                  <FluentEmoji char={cat.anchor} size={16} />
                </button>
              );
            })}
          </div>
        </div>
      )}

      {/* Grid */}
      <div className="px-3 pb-3 pt-1 overflow-y-auto">
        {emojis.length === 0 ? (
          <p className="text-center text-xs text-faint py-6">No match for "{query}"</p>
        ) : (
          <div className="grid grid-cols-7 gap-1">
            {emojis.map((char) => {
              const isSelected = char === current;
              return (
                <button
                  key={char}
                  type="button"
                  onClick={() => handlePick(char)}
                  className={`w-9 h-9 rounded-full flex items-center justify-center transition-colors active:scale-90 ${
                    isSelected ? "accent-tint-20 ring-1 ring-accent-50" : "hover:bg-input"
                  }`}
                  title={char}
                >
                  <FluentEmoji char={char} size={22} />
                </button>
              );
            })}
          </div>
        )}
      </div>
    </div>
  );

  return (
    <>
      {!anchorRect && <span ref={anchorRef} className="absolute bottom-0 right-0 w-0 h-0 pointer-events-none" />}
      {createPortal(picker, document.body)}
    </>
  );
}
