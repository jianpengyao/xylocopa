import { memo, useRef, useState } from "react";
import {
  Box, Check, Eye, EyeOff, FileSpreadsheet, FileText, Film, Globe,
  Image as ImageIcon, Info, Link2, Presentation, Server, Star,
} from "lucide-react";
import useLongPress from "../../hooks/useLongPress";
import { tileThumbUrl } from "./assetMedia";
import { assetWebapp, displayName, extBadge } from "./assetFilters";

/** Lucide glyph + tint for icon tiles (no thumbnail). */
export function AssetKindIcon({ asset, className = "w-7 h-7" }) {
  const app = assetWebapp(asset);
  const ext = (asset?.ext || "").toLowerCase();
  if (app?.kind === "port") return <Server className={`${className} text-accent`} strokeWidth={1.5} />;
  if (app?.kind === "url") return <Link2 className={`${className} text-accent`} strokeWidth={1.5} />;
  switch (asset?.kind) {
    case "webapp":
    case "html":
      return <Globe className={`${className} text-accent`} strokeWidth={1.5} />;
    case "pdf":
      return <FileText className={`${className} text-red-500`} strokeWidth={1.5} />;
    case "office":
      if (ext === "xlsx" || ext === "xls") return <FileSpreadsheet className={`${className} text-emerald-500`} strokeWidth={1.5} />;
      if (ext === "pptx" || ext === "ppt") return <Presentation className={`${className} text-orange-500`} strokeWidth={1.5} />;
      return <FileText className={`${className} text-blue-500`} strokeWidth={1.5} />;
    case "3d":
      return <Box className={`${className} text-violet-500`} strokeWidth={1.5} />;
    case "video":
      return <Film className={`${className} text-dim`} strokeWidth={1.5} />;
    case "image":
      return <ImageIcon className={`${className} text-dim`} strokeWidth={1.5} />;
    default:
      return <FileText className={`${className} text-dim`} strokeWidth={1.5} />;
  }
}

/**
 * Square grid tile for one asset.
 *
 * Tap (click) → onOpen (or onToggleSelect in selection mode); long-press →
 * onEnterSelect. The star corner button and the caption ⓘ are excluded from
 * the long-press/tap surface via data-no-longpress (AgentRow pattern).
 */
const AssetTile = memo(function AssetTile({
  project,
  asset,
  selecting = false,
  selected = false,
  onOpen,
  onToggleSelect,
  onEnterSelect,
  onToggleStar,
  onInfo,
}) {
  const thumb = tileThumbUrl(project, asset);
  // Keyed by URL so a new mtime (new _v) gets a fresh attempt.
  const [failedThumb, setFailedThumb] = useState(null);

  const missing = asset.exists === false;
  const isVideo = asset.kind === "video";
  const isSvg = (asset.ext || "").toLowerCase() === "svg";
  const name = displayName(asset);
  const showThumb = !!thumb && failedThumb !== thumb;

  const isInner = (e) => !!e?.target?.closest?.("[data-no-longpress]");
  const handleTap = () => {
    if (selecting) onToggleSelect?.(asset.path);
    else onOpen?.(asset);
  };
  // Tap runs from onClick, not useLongPress's pointerup: on touch the
  // browser's compatibility click lands ~5ms after pointerup, hit-testing
  // the overlay that tap just mounted (backdrop / lightbox container →
  // instant dismiss). useLongPress only drives the long-press here.
  const longFiredRef = useRef(false);
  const longPress = useLongPress((e) => {
    longFiredRef.current = true;
    if (selecting || isInner(e)) return;
    if (navigator.vibrate) navigator.vibrate(15);
    onEnterSelect?.(asset.path);
  });
  const onPointerDown = (e) => {
    longFiredRef.current = false;
    longPress.onPointerDown(e);
  };
  const onClick = (e) => {
    // Swallow the click that follows a long-press (it already selected).
    if (longFiredRef.current) { longFiredRef.current = false; return; }
    if (isInner(e)) return;
    handleTap();
  };

  return (
    <div className="min-w-0">
      <div
        role="button"
        tabIndex={0}
        aria-label={name}
        aria-pressed={selecting ? selected : undefined}
        title={asset.path}
        {...longPress}
        onPointerDown={onPointerDown}
        onClick={onClick}
        onKeyDown={(e) => {
          if (isInner(e)) return;
          if (e.key === "Enter" || e.key === " ") {
            e.preventDefault();
            handleTap();
          }
        }}
        style={{ WebkitTouchCallout: "none", WebkitTapHighlightColor: "transparent" }}
        className={`group relative aspect-square rounded-xl bg-surface overflow-hidden cursor-pointer select-none transition-[box-shadow,filter] duration-200 focus:outline-none focus-visible:ring-2 focus-visible:ring-accent ${
          selected ? "ring-2 ring-accent brightness-[0.88]" : "hover:ring-1 hover:ring-ring-hover"
        }`}
      >
        {/* Media / icon layer */}
        <div className={`absolute inset-0 ${missing ? "grayscale opacity-50" : asset.hidden ? "opacity-50" : ""}`}>
          {showThumb ? (
            <img
              src={thumb}
              alt=""
              loading="lazy"
              decoding="async"
              draggable={false}
              onError={() => setFailedThumb(thumb)}
              className={`chat-attachment-media w-full h-full pointer-events-none ${isSvg ? "object-contain p-3" : "object-cover"}`}
            />
          ) : (
            <div className="w-full h-full flex flex-col items-center justify-center gap-1.5 px-2">
              <AssetKindIcon asset={asset} />
              <span className="text-[10px] font-semibold tracking-wide text-faint truncate max-w-full">
                {extBadge(asset)}
              </span>
            </div>
          )}
        </div>

        {isVideo && showThumb && (
          <div className="absolute inset-0 flex items-center justify-center pointer-events-none">
            <div className="w-8 h-8 rounded-full bg-black/50 flex items-center justify-center">
              <svg className="w-4 h-4 ml-0.5 text-white" fill="currentColor" viewBox="0 0 24 24">
                <path d="M8 5v14l11-7z" />
              </svg>
            </div>
          </div>
        )}

        {missing && (
          <span className="absolute bottom-1.5 left-1.5 text-[9px] font-semibold uppercase tracking-wide px-1.5 py-px rounded bg-black/55 text-white/90 pointer-events-none">
            missing
          </span>
        )}

        {/* Top-left: selection check, else tier / hidden badges */}
        {selecting ? (
          <span
            className={`absolute top-1.5 left-1.5 w-6 h-6 rounded-full flex items-center justify-center pointer-events-none transition-colors ${
              selected ? "bg-accent text-white" : "bg-black/25 border-2 border-white/90"
            }`}
          >
            {selected && <Check className="w-4 h-4" strokeWidth={3} />}
          </span>
        ) : (asset.tier === "seen" || asset.hidden) && (
          <span className="absolute top-1.5 left-1.5 flex items-center gap-1 pointer-events-none">
            {asset.tier === "seen" && (
              <span title="Only seen by the agent (Read)" className="w-5 h-5 rounded-full bg-black/45 text-white/90 flex items-center justify-center">
                <Eye className="w-3 h-3" />
              </span>
            )}
            {asset.hidden && (
              <span title="Hidden" className="w-5 h-5 rounded-full bg-black/45 text-white/90 flex items-center justify-center">
                <EyeOff className="w-3 h-3" />
              </span>
            )}
          </span>
        )}

        {/* Top-right: quick star toggle (outline only on hover on pointer devices) */}
        {!selecting && (
          <button
            type="button"
            data-no-longpress
            onClick={(e) => { e.stopPropagation(); onToggleStar?.(asset); }}
            title={asset.starred ? "Unstar" : "Star"}
            aria-label={asset.starred ? "Unstar asset" : "Star asset"}
            className={`absolute top-1 right-1 w-7 h-7 rounded-full flex items-center justify-center bg-black/35 transition-opacity focus:outline-none focus-visible:opacity-100 focus-visible:ring-2 focus-visible:ring-accent ${
              asset.starred
                ? "text-amber-400"
                : "text-white/90 opacity-80 [@media(hover:hover)]:opacity-0 [@media(hover:hover)]:group-hover:opacity-100"
            }`}
          >
            <Star className="w-4 h-4" fill={asset.starred ? "currentColor" : "none"} strokeWidth={2} />
          </button>
        )}
      </div>

      {/* Caption + details affordance */}
      <div className="flex items-center gap-0.5 mt-1 pl-0.5">
        <p className={`text-[11px] leading-tight truncate min-w-0 flex-1 ${missing ? "text-faint" : "text-dim"}`} title={asset.path}>
          {name}
        </p>
        {!selecting && (
          <button
            type="button"
            onClick={(e) => { e.stopPropagation(); onInfo?.(asset); }}
            title="Details"
            aria-label={`Details for ${name}`}
            className="shrink-0 p-1 -my-1 rounded text-faint hover:text-heading hover:bg-input transition-colors"
          >
            <Info className="w-3.5 h-3.5" />
          </button>
        )}
      </div>
    </div>
  );
});

export default AssetTile;
