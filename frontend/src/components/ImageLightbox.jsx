import { useState, useRef, useCallback, useEffect } from "react";
import { createPortal } from "react-dom";
import { downloadFile } from "../lib/api";
import { useFileExists, withCacheBust } from "../lib/mediaState";
import MissingFileCard from "./MissingFileCard";
import {
  SWIPE_THRESHOLD, DISMISS_THRESHOLD,
  LIGHTBOX_DOUBLE_TAP_WINDOW, LIGHTBOX_DOUBLE_TAP_DIST,
  MAX_ZOOM_SCALE,
} from "../lib/constants";

// Must match the CSS transition duration in transformStyle / container style below
const TRANSITION_DURATION_MS = 250;

/**
 * Fullscreen media viewer with gesture support:
 * - Pinch-to-zoom, pan, double-tap toggle (images and videos)
 * - Videos: inline playback with play/pause overlay
 * - Swipe/arrow-key navigation across all media
 * - Swipe down to dismiss
 */
export default function ImageLightbox({ media, initialIndex = 0, onClose }) {
  const [currentIndex, setCurrentIndex] = useState(initialIndex);
  const [scale, setScale] = useState(1);
  const [translate, setTranslate] = useState({ x: 0, y: 0 });
  const [animating, setAnimating] = useState(false);
  const [dismissY, setDismissY] = useState(0);
  const [dismissOpacity, setDismissOpacity] = useState(1);
  const [dragging, setDragging] = useState(false);
  const [playing, setPlaying] = useState(false);
  const [videoError, setVideoError] = useState(null);
  const [hiresReady, setHiresReady] = useState({}); // { [index]: true } when full-res loaded
  const [entered, setEntered] = useState(false);
  const [closing, setClosing] = useState(false);
  const closingRef = useRef(false);

  // Stat the current file: drives the missing-file overlay and gives us
  // mtime to use as a cache-bust key (URL only changes when content does,
  // so the browser cache stays useful). `refresh` lets the missing card
  // re-probe — for the "file briefly gone, might be back" retry case.
  const currentSrc = media[currentIndex]?.src;
  const { stat: fileStat, refresh: refreshStat } = useFileExists(currentSrc);
  const isMissing = fileStat?.exists === false;
  const cacheVersion = fileStat?.mtime ?? null;

  const containerRef = useRef(null);
  const imgRef = useRef(null);
  const videoRef = useRef(null);

  // Gesture tracking refs — mutable state that doesn't trigger re-renders
  const touchState = useRef({
    lastTapTime: 0,
    lastTapPos: { x: 0, y: 0 },
    initialPinchDist: 0,
    initialScale: 1,
    initialTranslate: { x: 0, y: 0 },
    panStart: { x: 0, y: 0 },
    panStartTranslate: { x: 0, y: 0 },
    isPinching: false,
    isPanning: false,
    isSwiping: false,
    swipeStartY: 0,
    swipeStartX: 0,
    moved: false,
    moveStartX: 0,
    moveStartY: 0,
    singleTapTimer: null,
    touchOnInteractive: false,
  });

  // Keep current values in refs for event handlers (avoids stale closures)
  const scaleRef = useRef(scale);
  const translateRef = useRef(translate);
  const isZoomedRef = useRef(scale > 1.01);
  const currentIndexRef = useRef(currentIndex);
  const dismissYRef = useRef(dismissY);
  scaleRef.current = scale;
  translateRef.current = translate;
  isZoomedRef.current = scale > 1.01;
  currentIndexRef.current = currentIndex;
  dismissYRef.current = dismissY;

  const isZoomed = scale > 1.01;
  const isCurrentVideo = media[currentIndex]?.type === "video";

  // Clear animating flag when the CSS transition finishes (replaces setTimeout hacks)
  const handleTransitionEnd = useCallback((e) => {
    if (e.propertyName === "transform") setAnimating(false);
  }, []);

  // Reset transform when switching media
  const resetTransform = useCallback((animate = true) => {
    if (animate) setAnimating(true);
    setScale(1);
    setTranslate({ x: 0, y: 0 });
    setDismissY(0);
    setDismissOpacity(1);
    if (animate) setTimeout(() => setAnimating(false), TRANSITION_DURATION_MS);
  }, []);

  // Navigate to a different media item
  const goTo = useCallback(
    (index) => {
      if (index < 0 || index >= media.length) return;
      resetTransform(false);
      setCurrentIndex(index);
    },
    [media.length, resetTransform]
  );

  // Animated dismiss (exit animation then unmount)
  const handleDismiss = useCallback(() => {
    if (closingRef.current) return;
    closingRef.current = true;
    setClosing(true);
    setTimeout(() => onClose(), 220);
  }, [onClose]);

  // Pause video and reset error when navigating away
  useEffect(() => {
    if (videoRef.current) {
      videoRef.current.pause();
    }
    setPlaying(false);
    setVideoError(null);
  }, [currentIndex]);

  // Preload full-res image in background when thumbnail is shown.
  // Cache-busted by mtime so we refetch only when the file actually changed.
  useEffect(() => {
    const cur = media[currentIndex];
    if (!cur || cur.type === "video" || !cur.thumbSrc || hiresReady[currentIndex]) return;
    if (isMissing) return; // don't preload if server says it's gone
    const img = new Image();
    img.onload = () => setHiresReady((prev) => ({ ...prev, [currentIndex]: true }));
    img.src = withCacheBust(cur.src, cacheVersion);
    return () => { img.onload = null; };
  }, [currentIndex, media, hiresReady, cacheVersion, isMissing]);

  // Clamp translate so image doesn't go off-screen too far
  const clampTranslate = useCallback(
    (tx, ty, s) => {
      if (s <= 1) return { x: 0, y: 0 };
      const container = containerRef.current;
      if (!container) return { x: tx, y: ty };
      const cw = container.clientWidth;
      const ch = container.clientHeight;
      const maxX = Math.max(0, (cw * (s - 1)) / 2);
      const maxY = Math.max(0, (ch * (s - 1)) / 2);
      return {
        x: Math.max(-maxX, Math.min(maxX, tx)),
        y: Math.max(-maxY, Math.min(maxY, ty)),
      };
    },
    []
  );

  const fingerDist = (t1, t2) =>
    Math.hypot(t2.clientX - t1.clientX, t2.clientY - t1.clientY);

  const midpoint = (t1, t2) => ({
    x: (t1.clientX + t2.clientX) / 2,
    y: (t1.clientY + t2.clientY) / 2,
  });

  // --- Attach non-passive touch listeners via ref (needed for preventDefault) ---
  useEffect(() => {
    const el = containerRef.current;
    if (!el) return;

    const onTouchStart = (e) => {
      const ts = touchState.current;
      ts.moved = false;
      // Track whether touch started on an interactive element (button, video, link)
      ts.touchOnInteractive = !!e.target.closest?.("button, video, a, input, [role='button']");
      if (e.touches.length >= 1) {
        ts.moveStartX = e.touches[0].clientX;
        ts.moveStartY = e.touches[0].clientY;
      }

      if (e.touches.length === 2) {
        ts.isPinching = true;
        ts.isPanning = false;
        ts.isSwiping = false;
        ts.initialPinchDist = fingerDist(e.touches[0], e.touches[1]);
        ts.initialScale = scaleRef.current;
        ts.initialTranslate = { ...translateRef.current };
      } else if (e.touches.length === 1) {
        ts.isPinching = false;
        const touch = e.touches[0];
        ts.panStart = { x: touch.clientX, y: touch.clientY };
        ts.panStartTranslate = { ...translateRef.current };
        ts.swipeStartX = touch.clientX;
        ts.swipeStartY = touch.clientY;

        if (isZoomedRef.current) {
          ts.isPanning = true;
          ts.isSwiping = false;
        } else if (!ts.touchOnInteractive) {
          // Only enable swiping if not on an interactive element
          ts.isPanning = false;
          ts.isSwiping = true;
        } else {
          ts.isPanning = false;
          ts.isSwiping = false;
        }
      }
    };

    const onTouchMove = (e) => {
      const ts = touchState.current;
      // Don't preventDefault for interactive elements (let native video controls etc. work)
      if (!ts.touchOnInteractive || ts.isPinching) {
        e.preventDefault();
      }
      if (e.touches.length >= 1) {
        const mdx = e.touches[0].clientX - ts.moveStartX;
        const mdy = e.touches[0].clientY - ts.moveStartY;
        if (Math.hypot(mdx, mdy) > 8) ts.moved = true;
      }

      if (ts.isPinching && e.touches.length === 2) {
        const newDist = fingerDist(e.touches[0], e.touches[1]);
        const ratio = newDist / ts.initialPinchDist;
        const newScale = Math.max(1, Math.min(MAX_ZOOM_SCALE, ts.initialScale * ratio));

        const mid = midpoint(e.touches[0], e.touches[1]);
        const container = containerRef.current;
        if (container) {
          const rect = container.getBoundingClientRect();
          const cx = mid.x - rect.left - rect.width / 2;
          const cy = mid.y - rect.top - rect.height / 2;
          const scaleChange = newScale / ts.initialScale;
          const tx = ts.initialTranslate.x + cx - cx * scaleChange;
          const ty = ts.initialTranslate.y + cy - cy * scaleChange;
          setTranslate(clampTranslate(tx, ty, newScale));
        }
        setScale(newScale);
      } else if (ts.isPanning && e.touches.length === 1) {
        const touch = e.touches[0];
        const dx = touch.clientX - ts.panStart.x;
        const dy = touch.clientY - ts.panStart.y;
        const tx = ts.panStartTranslate.x + dx;
        const ty = ts.panStartTranslate.y + dy;
        setTranslate(clampTranslate(tx, ty, scaleRef.current));
      } else if (ts.isSwiping && e.touches.length === 1) {
        const touch = e.touches[0];
        const dy = touch.clientY - ts.swipeStartY;
        setDismissY(dy);
        setDismissOpacity(Math.max(0.2, 1 - Math.abs(dy) / 300));
      }
    };

    const onTouchEnd = (e) => {
      const ts = touchState.current;

      if (ts.isPinching) {
        ts.isPinching = false;
        if (scaleRef.current < 1.05) {
          setAnimating(true);
          setScale(1);
          setTranslate({ x: 0, y: 0 });
          setTimeout(() => setAnimating(false), TRANSITION_DURATION_MS);
        }
        return;
      }

      if (ts.isSwiping && e.changedTouches.length === 1) {
        const touch = e.changedTouches[0];
        const dx = touch.clientX - ts.swipeStartX;
        const dy = touch.clientY - ts.swipeStartY;

        // Swipe up or down to dismiss
        if (Math.abs(dy) > DISMISS_THRESHOLD && Math.abs(dx) < Math.abs(dy)) {
          handleDismiss();
          return;
        }

        // Reset dismiss feedback
        if (dismissYRef.current !== 0) {
          setAnimating(true);
          setDismissY(0);
          setDismissOpacity(1);
          setTimeout(() => setAnimating(false), TRANSITION_DURATION_MS);
        }

        // Swipe left/right to navigate
        if (media.length > 1 && Math.abs(dx) > SWIPE_THRESHOLD && Math.abs(dy) < SWIPE_THRESHOLD) {
          if (dx < -SWIPE_THRESHOLD) goTo(currentIndexRef.current + 1);
          else if (dx > SWIPE_THRESHOLD) goTo(currentIndexRef.current - 1);
          return;
        }
      }

      // Tap detection: single-tap = delayed close (cancellable by double-tap), double-tap = zoom
      // Skip tap handling for interactive elements (video controls, buttons, links)
      if (!ts.moved && e.changedTouches.length === 1 && !ts.touchOnInteractive) {
        const touch = e.changedTouches[0];
        const now = Date.now();
        const dt = now - ts.lastTapTime;
        const tapDist = Math.hypot(
          touch.clientX - ts.lastTapPos.x,
          touch.clientY - ts.lastTapPos.y
        );

        if (dt < LIGHTBOX_DOUBLE_TAP_WINDOW && tapDist < LIGHTBOX_DOUBLE_TAP_DIST) {
          // Double-tap: zoom toggle (cancel pending single-tap dismiss)
          ts.lastTapTime = 0;
          clearTimeout(ts.singleTapTimer);
          setAnimating(true);
          if (isZoomedRef.current) {
            setScale(1);
            setTranslate({ x: 0, y: 0 });
          } else {
            const container = containerRef.current;
            if (container) {
              const rect = container.getBoundingClientRect();
              const tapX = touch.clientX - rect.left - rect.width / 2;
              const tapY = touch.clientY - rect.top - rect.height / 2;
              const newScale = 2;
              const tx = -tapX * (newScale - 1);
              const ty = -tapY * (newScale - 1);
              setScale(newScale);
              setTranslate(clampTranslate(tx, ty, newScale));
            } else {
              setScale(2);
            }
          }
          setTimeout(() => setAnimating(false), TRANSITION_DURATION_MS);
        } else {
          ts.lastTapTime = now;
          ts.lastTapPos = { x: touch.clientX, y: touch.clientY };
          clearTimeout(ts.singleTapTimer);
          if (isZoomedRef.current) {
            // Reset zoom on single tap when zoomed
            setAnimating(true);
            setScale(1);
            setTranslate({ x: 0, y: 0 });
            setTimeout(() => setAnimating(false), TRANSITION_DURATION_MS);
          } else {
            // Delay dismiss so a second tap (double-tap) can cancel it
            ts.singleTapTimer = setTimeout(() => {
              handleDismiss();
            }, LIGHTBOX_DOUBLE_TAP_WINDOW);
          }
        }
      }

      ts.isPanning = false;
      ts.isSwiping = false;
    };

    el.addEventListener("touchstart", onTouchStart, { passive: true });
    el.addEventListener("touchmove", onTouchMove, { passive: false });
    el.addEventListener("touchend", onTouchEnd, { passive: true });

    return () => {
      el.removeEventListener("touchstart", onTouchStart);
      el.removeEventListener("touchmove", onTouchMove);
      el.removeEventListener("touchend", onTouchEnd);
      clearTimeout(touchState.current.singleTapTimer);
    };
  }, [clampTranslate, goTo, media.length, handleDismiss]);

  // Wheel handler: pinch-to-zoom, trackpad swipe navigation, and pan
  useEffect(() => {
    const swipeAccum = { x: 0, timer: null, locked: false };

    const onWheel = (e) => {
      e.preventDefault();

      // Ctrl+wheel / Cmd+wheel = trackpad pinch = zoom
      if (e.ctrlKey || e.metaKey) {
        const oldScale = scaleRef.current;
        const newScale = Math.max(1, Math.min(MAX_ZOOM_SCALE, oldScale * Math.pow(2, -e.deltaY / 100)));
        const container = containerRef.current;
        if (!container) return;
        const rect = container.getBoundingClientRect();
        const cx = e.clientX - rect.left - rect.width / 2;
        const cy = e.clientY - rect.top - rect.height / 2;
        const ratio = newScale / oldScale;
        const tx = translateRef.current.x + cx - cx * ratio;
        const ty = translateRef.current.y + cy - cy * ratio;
        setScale(newScale);
        setTranslate(newScale <= 1 ? { x: 0, y: 0 } : clampTranslate(tx, ty, newScale));
        return;
      }

      // When zoomed: two-finger scroll = pan
      if (scaleRef.current > 1.01) {
        const tx = translateRef.current.x - e.deltaX;
        const ty = translateRef.current.y - e.deltaY;
        setTranslate(clampTranslate(tx, ty, scaleRef.current));
        return;
      }

      // At scale=1: accumulate horizontal deltaX for swipe navigation
      if (media.length > 1 && !swipeAccum.locked && Math.abs(e.deltaX) > Math.abs(e.deltaY)) {
        swipeAccum.x += e.deltaX;
        clearTimeout(swipeAccum.timer);
        swipeAccum.timer = setTimeout(() => { swipeAccum.x = 0; }, LIGHTBOX_DOUBLE_TAP_WINDOW);
        if (swipeAccum.x > SWIPE_THRESHOLD) {
          goTo(currentIndexRef.current + 1);
          swipeAccum.x = 0;
          swipeAccum.locked = true;
          setTimeout(() => { swipeAccum.locked = false; }, LIGHTBOX_DOUBLE_TAP_WINDOW);
        } else if (swipeAccum.x < -SWIPE_THRESHOLD) {
          goTo(currentIndexRef.current - 1);
          swipeAccum.x = 0;
          swipeAccum.locked = true;
          setTimeout(() => { swipeAccum.locked = false; }, LIGHTBOX_DOUBLE_TAP_WINDOW);
        }
      }
    };

    window.addEventListener("wheel", onWheel, { passive: false });
    return () => {
      window.removeEventListener("wheel", onWheel);
      clearTimeout(swipeAccum.timer);
    };
  }, [clampTranslate, goTo, media.length]);

  // Mouse drag panning when zoomed in
  useEffect(() => {
    const mouseState = { active: false, startX: 0, startY: 0, startTx: 0, startTy: 0 };

    const onMouseDown = (e) => {
      if (e.button !== 0 || scaleRef.current <= 1) return;
      e.preventDefault();
      mouseState.active = true;
      mouseState.startX = e.clientX;
      mouseState.startY = e.clientY;
      mouseState.startTx = translateRef.current.x;
      mouseState.startTy = translateRef.current.y;
      setDragging(true);
    };

    const onMouseMove = (e) => {
      if (!mouseState.active) return;
      const tx = mouseState.startTx + (e.clientX - mouseState.startX);
      const ty = mouseState.startTy + (e.clientY - mouseState.startY);
      setTranslate(clampTranslate(tx, ty, scaleRef.current));
    };

    const onMouseUp = () => {
      if (!mouseState.active) return;
      mouseState.active = false;
      setDragging(false);
    };

    window.addEventListener("mousedown", onMouseDown);
    window.addEventListener("mousemove", onMouseMove);
    window.addEventListener("mouseup", onMouseUp);

    return () => {
      window.removeEventListener("mousedown", onMouseDown);
      window.removeEventListener("mousemove", onMouseMove);
      window.removeEventListener("mouseup", onMouseUp);
    };
  }, [clampTranslate]);

  // Keyboard: Escape, arrow keys, Cmd+=/Cmd+-/Cmd+0
  useEffect(() => {
    const handler = (e) => {
      if (e.key === "Escape") {
        handleDismiss();
        return;
      }

      // Arrow keys navigate between media (resets zoom via goTo)
      if (e.key === "ArrowLeft") {
        e.preventDefault();
        goTo(currentIndexRef.current - 1);
        return;
      }
      if (e.key === "ArrowRight") {
        e.preventDefault();
        goTo(currentIndexRef.current + 1);
        return;
      }

      // Cmd+= / Cmd+- / Cmd+0 — zoom
      if (e.metaKey || e.ctrlKey) {
        if (e.key === "=" || e.key === "+") {
          e.preventDefault();
          const newScale = Math.min(MAX_ZOOM_SCALE, scaleRef.current * 1.25);
          setScale(newScale);
          setTranslate(clampTranslate(translateRef.current.x, translateRef.current.y, newScale));
        } else if (e.key === "-") {
          e.preventDefault();
          const newScale = Math.max(1, scaleRef.current / 1.25);
          if (newScale <= 1.05) {
            setScale(1);
            setTranslate({ x: 0, y: 0 });
          } else {
            setScale(newScale);
            setTranslate(clampTranslate(translateRef.current.x, translateRef.current.y, newScale));
          }
        } else if (e.key === "0") {
          e.preventDefault();
          setAnimating(true);
          setScale(1);
          setTranslate({ x: 0, y: 0 });
          setTimeout(() => setAnimating(false), TRANSITION_DURATION_MS);
        }
      }
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [handleDismiss, clampTranslate, goTo]);

  // Prevent body scroll when lightbox is open
  useEffect(() => {
    document.body.style.overflow = "hidden";
    return () => {
      document.body.style.overflow = "";
    };
  }, []);

  // Entrance animation
  useEffect(() => {
    requestAnimationFrame(() => requestAnimationFrame(() => setEntered(true)));
  }, []);

  const current = media[currentIndex];
  if (!current) return null;

  const transformStyle = {
    transform: `translate(${translate.x}px, ${translate.y + dismissY}px) scale(${scale})`,
    transition: animating ? `transform ${TRANSITION_DURATION_MS}ms ease-out` : "none",
    willChange: "transform",
  };

  return createPortal(
    <div
      ref={containerRef}
      className="fixed inset-0 z-50 flex items-center justify-center select-none"
      style={{
        backgroundColor: `rgba(0,0,0,${(!entered || closing) ? 0 : dismissOpacity * 0.95})`,
        transition: `background-color 280ms cubic-bezier(0.22, 1.15, 0.36, 1)`,
        touchAction: "none",
        cursor: isZoomed ? (dragging ? "grabbing" : "grab") : "default",
      }}
      onClick={(e) => {
        if (!isZoomed && !touchState.current.moved && e.target === containerRef.current) {
          handleDismiss();
        }
      }}
    >
      {/* Download button (full-res). Hidden when the file is gone — no
          point downloading a 404 JSON body. */}
      {!isCurrentVideo && !isMissing && (
        <button
          type="button"
          onClick={(e) => {
            e.stopPropagation();
            downloadFile(current.src, current.filename || "image");
          }}
          className="absolute top-4 right-18 z-10 w-10 h-10 flex items-center justify-center rounded-full bg-white/10 text-white/80 hover:bg-white/20 transition-colors"
          style={{ marginTop: "env(safe-area-inset-top, 0px)" }}
        >
          <svg className="w-5 h-5" fill="none" stroke="currentColor" strokeWidth={2} viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" d="M3 16.5v2.25A2.25 2.25 0 005.25 21h13.5A2.25 2.25 0 0021 18.75V16.5M16.5 12L12 16.5m0 0L7.5 12m4.5 4.5V3" />
          </svg>
        </button>
      )}

      {/* Close button */}
      <button
        type="button"
        onClick={(e) => {
          e.stopPropagation();
          handleDismiss();
        }}
        className="absolute top-4 right-4 z-10 w-10 h-10 flex items-center justify-center rounded-full bg-white/10 text-white/80 hover:bg-white/20 transition-colors"
        style={{ marginTop: "env(safe-area-inset-top, 0px)" }}
      >
        <svg
          className="w-6 h-6"
          fill="none"
          stroke="currentColor"
          strokeWidth={2}
          viewBox="0 0 24 24"
        >
          <path
            strokeLinecap="round"
            strokeLinejoin="round"
            d="M6 18L18 6M6 6l12 12"
          />
        </svg>
      </button>

      {/* Media counter */}
      {media.length > 1 && (
        <div
          className="absolute top-4 left-1/2 -translate-x-1/2 z-10 px-3 py-1 rounded-full bg-black/50 text-white/80 text-xs font-medium"
          style={{ marginTop: "env(safe-area-inset-top, 0px)" }}
        >
          {currentIndex + 1} / {media.length}
        </div>
      )}

      {/* Media content */}
      <div style={{
        opacity: (!entered || closing) ? 0 : 1,
        transform: (!entered || closing) ? "scale(0.92)" : "scale(1)",
        transition: "opacity 280ms cubic-bezier(0.22, 1.15, 0.36, 1), transform 280ms cubic-bezier(0.22, 1.15, 0.36, 1)",
        willChange: "opacity, transform",
      }}>
      {isMissing ? (
        <MissingFileCard filename={current.filename || ""} dark onRetry={refreshStat} />
      ) : isCurrentVideo ? (
        // Poster: prefer the /api/thumbs frame (thumbSrc); the legacy sibling
        // `<video>.thumb.jpg` must go before the query string or it corrupts ?token=.
        <video
          ref={videoRef}
          key={current.src}
          src={current.src}
          poster={current.thumbSrc || current.src.replace(/(\?|$)/, ".thumb.jpg$1")}
          preload="auto"
          playsInline
          webkit-playsinline=""
          controls
          onPlay={() => setPlaying(true)}
          onPause={() => setPlaying(false)}
          onEnded={() => setPlaying(false)}
          onError={(e) => {
            const err = e.target.error;
            setVideoError(err ? `${err.message || "Cannot play this video"}` : "Video failed to load");
          }}
          className="chat-attachment-media max-h-[90vh] max-w-[90vw] object-contain select-none"
          style={transformStyle}
          onTransitionEnd={handleTransitionEnd}
        />
      ) : (
        <img
          ref={imgRef}
          src={withCacheBust(current.thumbSrc && !hiresReady[currentIndex] ? current.thumbSrc : current.src, cacheVersion)}
          alt={current.filename || ""}
          draggable={false}
          className="chat-attachment-media max-h-[90vh] max-w-[90vw] object-contain pointer-events-none select-none"
          style={transformStyle}
          onTransitionEnd={handleTransitionEnd}
        />
      )}
      </div>

      {/* Error overlay for videos (codec/playback errors — distinct from
          isMissing which catches a deleted/never-existed file). */}
      {isCurrentVideo && videoError && !isMissing && (
        <div className="absolute bottom-20 left-1/2 -translate-x-1/2 z-10 px-4 py-2 rounded-lg bg-red-500/80 text-white text-sm max-w-[80vw] text-center">
          {videoError}
        </div>
      )}

      {/* Navigation dots */}
      {media.length > 1 && (
        <div
          className="absolute bottom-6 left-1/2 -translate-x-1/2 z-10 flex items-center gap-1.5"
          style={{}}
        >
          {media.map((_, i) => (
            <div
              key={i}
              className={`rounded-full transition-all ${
                i === currentIndex
                  ? "w-2 h-2 bg-white"
                  : "w-1.5 h-1.5 bg-white/40"
              }`}
            />
          ))}
        </div>
      )}
    </div>,
    document.body
  );
}
