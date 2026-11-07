import { useState, useEffect, useLayoutEffect, useRef, useMemo, useCallback } from "react";
import { createPortal } from "react-dom";
import { designPx } from "../lib/textScale";

const DAY_LABELS = ["Su", "Mo", "Tu", "We", "Th", "Fr", "Sa"];
const MONTHS = ["January", "February", "March", "April", "May", "June",
  "July", "August", "September", "October", "November", "December"];

function Stepper({ value, min, max, onUp, onDown, onChange, padTo = 0, width = "w-6" }) {
  const display = padTo > 0 ? String(value).padStart(padTo, "0") : String(value);
  const handleInputChange = (e) => {
    // Strip non-digits; keep last 2 digits typed (so "346" → 46).
    const raw = e.target.value.replace(/\D/g, "").slice(-2);
    if (raw === "") return;
    let n = parseInt(raw, 10);
    if (isNaN(n)) return;
    if (n < min) n = min;
    if (n > max) n = max;
    onChange(n);
  };
  const handleKeyDown = (e) => {
    if (e.key === "ArrowUp") { e.preventDefault(); onUp(); }
    else if (e.key === "ArrowDown") { e.preventDefault(); onDown(); }
    else if (e.key === "Enter") { e.preventDefault(); e.target.blur(); }
  };
  return (
    <div className="flex flex-col items-center">
      <button type="button" onClick={onUp}
        className="w-7 h-5 rounded-t-md bg-input hover:bg-hover flex items-center justify-center text-dim hover:text-heading transition-colors active:scale-95">
        <svg className="w-3 h-3" fill="none" stroke="currentColor" strokeWidth={2.5} viewBox="0 0 24 24">
          <path strokeLinecap="round" strokeLinejoin="round" d="M5 15l7-7 7 7" />
        </svg>
      </button>
      <input
        type="text"
        inputMode="numeric"
        value={display}
        onChange={handleInputChange}
        onFocus={(e) => e.target.select()}
        onKeyDown={handleKeyDown}
        className={`text-sm font-semibold text-heading ${width} text-center leading-6 bg-transparent outline-none focus:bg-input rounded-sm`}
      />
      <button type="button" onClick={onDown}
        className="w-7 h-5 rounded-b-md bg-input hover:bg-hover flex items-center justify-center text-dim hover:text-heading transition-colors active:scale-95">
        <svg className="w-3 h-3" fill="none" stroke="currentColor" strokeWidth={2.5} viewBox="0 0 24 24">
          <path strokeLinecap="round" strokeLinejoin="round" d="M19 9l-7 7-7-7" />
        </svg>
      </button>
    </div>
  );
}

export default function SendLaterPicker({ onSelect, onClose, onClear, title = "Remind At" }) {
  const anchorRef = useRef(null);
  const pickerRef = useRef(null);
  const [pos, setPos] = useState(null);

  const now = useMemo(() => new Date(), []);
  const [viewYear, setViewYear] = useState(now.getFullYear());
  const [viewMonth, setViewMonth] = useState(now.getMonth());
  // Default to today + current time so a user wanting "soon" only has to
  // step the minute (or click confirm after one nudge). Centralizing this
  // here means every call site (defer, notify_at, scheduled message) gets
  // the same behavior.
  const [selectedDay, setSelectedDay] = useState(() => ({
    year: now.getFullYear(),
    month: now.getMonth(),
    day: now.getDate(),
  }));
  const [hour12, setHour12] = useState(() => {
    const h = now.getHours() % 12;
    return h === 0 ? 12 : h;
  });
  const [minute, setMinute] = useState(() => now.getMinutes());
  const [ampm, setAmpm] = useState(() => now.getHours() >= 12 ? "PM" : "AM");

  // Convert 12h → 24h for final output
  const to24h = () => {
    if (ampm === "AM") return hour12 === 12 ? 0 : hour12;
    return hour12 === 12 ? 12 : hour12 + 12;
  };

  // Position picker above anchor, respecting screen edges
  useLayoutEffect(() => {
    if (!anchorRef.current) return;
    const rect = anchorRef.current.getBoundingClientRect();
    // Design px of the w-[280px] picker, scaled like the CSS (Settings > Text size).
    const pickerW = designPx(280);
    const pickerH = designPx(440);
    let right = window.innerWidth - rect.right;
    if (rect.right - pickerW < 8) right = window.innerWidth - pickerW - 8;
    if (right < 8) right = 8;
    const spaceAbove = rect.top;
    if (spaceAbove < pickerH + 8) {
      setPos({ top: rect.bottom + 4, right });
    } else {
      setPos({ bottom: window.innerHeight - rect.top + 4, right });
    }
  }, []);

  // Close on outside click
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

  // Calendar grid for current view month
  const calendarDays = useMemo(() => {
    const firstDay = new Date(viewYear, viewMonth, 1).getDay();
    const daysInMonth = new Date(viewYear, viewMonth + 1, 0).getDate();
    const cells = [];
    for (let i = 0; i < firstDay; i++) cells.push(null);
    for (let d = 1; d <= daysInMonth; d++) cells.push(d);
    return cells;
  }, [viewYear, viewMonth]);

  const isToday = useCallback((day) => {
    return day && viewYear === now.getFullYear() && viewMonth === now.getMonth() && day === now.getDate();
  }, [viewYear, viewMonth, now]);

  const isSelected = useCallback((day) => {
    if (!day || !selectedDay) return false;
    return selectedDay.year === viewYear && selectedDay.month === viewMonth && selectedDay.day === day;
  }, [selectedDay, viewYear, viewMonth]);

  const isPast = useCallback((day) => {
    if (!day) return false;
    const d = new Date(viewYear, viewMonth, day, 23, 59, 59);
    return d < now;
  }, [viewYear, viewMonth, now]);

  const prevMonth = () => {
    if (viewMonth === 0) { setViewMonth(11); setViewYear(y => y - 1); }
    else setViewMonth(m => m - 1);
  };
  const nextMonth = () => {
    if (viewMonth === 11) { setViewMonth(0); setViewYear(y => y + 1); }
    else setViewMonth(m => m + 1);
  };

  const handlePreset = (date) => onSelect(date.toISOString());

  const handle3Hours = () => {
    handlePreset(new Date(Date.now() + 3 * 3600000));
  };

  const handleTomorrow = () => {
    const d = new Date();
    d.setDate(d.getDate() + 1);
    d.setHours(9, 0, 0, 0);
    handlePreset(d);
  };

  const handleNextWeek = () => {
    const d = new Date();
    d.setDate(d.getDate() + 7);
    d.setHours(9, 0, 0, 0);
    handlePreset(d);
  };

  const handleDayClick = (day) => {
    if (!day || isPast(day)) return;
    setSelectedDay({ year: viewYear, month: viewMonth, day });
  };

  // Recomputes when day/time changes so confirm enables exactly when the
  // selected moment is in the future. Replaces the old silent past-check
  // in handleConfirm (which left users wondering why the button "didn't
  // work") with a visibly-disabled confirm.
  const selectedIsFuture = useMemo(() => {
    if (!selectedDay) return false;
    const d = new Date(selectedDay.year, selectedDay.month, selectedDay.day, to24h(), minute, 0, 0);
    return d > new Date();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedDay, hour12, minute, ampm]);

  const handleConfirm = () => {
    if (!selectedIsFuture) return;
    const d = new Date(selectedDay.year, selectedDay.month, selectedDay.day, to24h(), minute, 0, 0);
    onSelect(d.toISOString());
  };

  const cycleHour = (delta) => setHour12(h => ((h - 1 + delta + 12) % 12) + 1);
  const cycleMinute = (delta) => setMinute(m => (m + delta + 60) % 60);

  const posStyle = pos
    ? { position: "fixed", ...pos }
    : { visibility: "hidden", position: "fixed" };

  const picker = (
    <div
      ref={pickerRef}
      data-card
      className="w-[280px] bg-surface border border-divider rounded-2xl shadow-xl overflow-hidden z-[9999]"
      style={posStyle}
    >
      {/* Header */}
      <div className="px-4 py-2.5 flex items-center justify-between border-b border-divider">
        <span className="text-sm font-semibold text-heading">{title}</span>
        <div className="flex items-center gap-2">
          {onClear && (
            <button type="button" onClick={() => { onClear(); onClose(); }}
              className="text-xs text-red-400 hover:text-red-300 font-medium transition-colors">
              Clear
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

      {/* Presets */}
      <div className="py-1 border-b border-divider">
        <button type="button" onClick={handle3Hours}
          className="w-full text-left px-4 py-2 text-sm text-body hover:bg-input transition-colors flex items-center gap-2.5">
          <svg className="w-4 h-4 text-amber-400 shrink-0" fill="none" stroke="currentColor" strokeWidth={2} viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" d="M12 6v6l4 2m6-2a10 10 0 11-20 0 10 10 0 0120 0z" />
          </svg>
          3 Hours
        </button>
        <button type="button" onClick={handleTomorrow}
          className="w-full text-left px-4 py-2 text-sm text-body hover:bg-input transition-colors flex items-center gap-2.5">
          <svg className="w-4 h-4 text-orange-400 shrink-0" fill="none" stroke="currentColor" strokeWidth={2} viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" d="M12 3v1m0 16v1m9-9h-1M4 12H3m15.364 6.364l-.707-.707M6.343 6.343l-.707-.707m12.728 0l-.707.707M6.343 17.657l-.707.707M16 12a4 4 0 11-8 0 4 4 0 018 0z" />
          </svg>
          Tomorrow
        </button>
        <button type="button" onClick={handleNextWeek}
          className="w-full text-left px-4 py-2 text-sm text-body hover:bg-input transition-colors flex items-center gap-2.5">
          <svg className="w-4 h-4 text-accent shrink-0" fill="none" stroke="currentColor" strokeWidth={2} viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" d="M8 7V3m8 4V3m-9 8h10M5 21h14a2 2 0 002-2V7a2 2 0 00-2-2H5a2 2 0 00-2 2v12a2 2 0 002 2z" />
          </svg>
          Next Week
        </button>
      </div>

      {/* Calendar */}
      <div className="px-3 pt-3 pb-2">
        {/* Month nav */}
        <div className="flex items-center justify-between mb-2 px-1">
          <span className="text-xs font-semibold text-heading">
            {MONTHS[viewMonth]} {viewYear}
          </span>
          <div className="flex items-center gap-1">
            <button type="button" onClick={prevMonth}
              className="w-6 h-6 rounded-full hover:bg-input flex items-center justify-center text-dim hover:text-heading transition-colors">
              <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" strokeWidth={2.5} viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" d="M15 19l-7-7 7-7" />
              </svg>
            </button>
            <button type="button" onClick={nextMonth}
              className="w-6 h-6 rounded-full hover:bg-input flex items-center justify-center text-dim hover:text-heading transition-colors">
              <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" strokeWidth={2.5} viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" d="M9 5l7 7-7 7" />
              </svg>
            </button>
          </div>
        </div>

        {/* Day headers */}
        <div className="grid grid-cols-7 mb-1">
          {DAY_LABELS.map(d => (
            <div key={d} className="text-center text-[10px] font-medium text-faint py-0.5">{d}</div>
          ))}
        </div>

        {/* Day cells */}
        <div className="grid grid-cols-7">
          {calendarDays.map((day, i) => (
            <button
              key={i}
              type="button"
              disabled={!day || isPast(day)}
              onClick={() => handleDayClick(day)}
              className={`h-8 text-xs rounded-full flex items-center justify-center transition-colors ${
                !day ? ""
                  : isPast(day) ? "text-faint/30 cursor-not-allowed"
                  : isSelected(day) ? "bg-accent text-white font-semibold"
                  : isToday(day) ? "accent-tint-15 text-accent font-semibold hover:accent-tint-25"
                  : "text-body hover:bg-input"
              }`}
            >
              {day || ""}
            </button>
          ))}
        </div>
      </div>

      {/* Time + Confirm */}
      <div className="border-t border-divider px-4 py-3 flex items-center justify-between">
        <div className="flex items-center gap-1.5">
          <span className="text-xs font-medium text-label mr-0.5">Time</span>
          <Stepper
            value={hour12}
            min={1}
            max={12}
            onUp={() => cycleHour(1)}
            onDown={() => cycleHour(-1)}
            onChange={setHour12}
          />
          <span className="text-sm font-semibold text-dim leading-6">:</span>
          <Stepper
            value={minute}
            min={0}
            max={59}
            padTo={2}
            onUp={() => cycleMinute(1)}
            onDown={() => cycleMinute(-1)}
            onChange={setMinute}
          />
          <button type="button" onClick={() => setAmpm(v => v === "AM" ? "PM" : "AM")}
            className="ml-1 px-2 py-1 rounded-md bg-input hover:bg-hover text-xs font-semibold text-heading transition-colors active:scale-95">
            {ampm}
          </button>
        </div>
        <button
          type="button"
          onClick={handleConfirm}
          disabled={!selectedIsFuture}
          className={`w-8 h-8 rounded-full flex items-center justify-center transition-all active:scale-90 ${
            selectedIsFuture ? "bg-accent text-accent-ink hover:opacity-90" : "bg-elevated text-faint cursor-not-allowed"
          }`}
          title="Confirm"
        >
          <svg className="w-5 h-5" fill="none" stroke="currentColor" strokeWidth={2.5} viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" d="M5 13l4 4L19 7" />
          </svg>
        </button>
      </div>
    </div>
  );

  return (
    <>
      <span ref={anchorRef} className="absolute bottom-0 right-0 w-0 h-0 pointer-events-none" />
      {createPortal(picker, document.body)}
    </>
  );
}
