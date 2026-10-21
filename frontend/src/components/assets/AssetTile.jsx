import { memo, useRef, useState } from "react";
import {
  Box, Check, ExternalLink, Eye, EyeOff, FileSpreadsheet, FileText, Film, Globe,
  Image as ImageIcon, LayoutDashboard, Link2, MoreHorizontal, PanelsTopLeft,
  Presentation, Server, Sparkles, Star,
} from "lucide-react";
import useLongPress from "../../hooks/useLongPress";
import { relativeTime } from "../../lib/formatters";
import { tileThumbUrl } from "./assetMedia";
import { assetArtifact, assetWebapp, displayName, extBadge } from "./assetFilters";

// claude.ai artifact glyph by artifact.kind; all accent-tinted so a Slides
// artifact reads differently from an (orange) .pptx file.
const ARTIFACT_ICONS = {
  slides: Presentation,
  doc: FileText,
  page: PanelsTopLeft,
  dashboard: LayoutDashboard,
};

/** Lucide glyph + tint for icon tiles (no thumbnail). */
export function AssetKindIcon({ asset, className = "w-7 h-7" }) {
  const art = assetArtifact(asset);
  if (art) {
    const Icon = ARTIFACT_ICONS[art.kind] || Sparkles;
    return <Icon className={`${className} text-accent`} strokeWidth={1.5} />;
  }
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

/** File name with soft-wrap points after "_" / "-" and before ".", so a
 *  two-line caption breaks between name parts rather than mid-word. */
function softWrapName(name) {
  const parts = [];
  let cur = "";
  for (const ch of name) {
    if (ch === "." && cur) { parts.push(cur); cur = ""; }
    cur += ch;
    if (ch === "_" || ch === "-") { parts.push(cur); cur = ""; }
  }
  if (cur) parts.push(cur);
  return parts.flatMap((p, i) => (i ? [<wbr key={i} />, p] : [p]));
}

/**
 * Square grid tile for one asset.
 *
 * Tap (click) → onOpen (or onToggleSelect in selection mode); long-press →
 * onEnterSelect. The star corner button and the caption ⋯ are excluded from
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
  // claude.ai artifact: a link card (never a thumbnail); hover shows the URL.
  const art = assetArtifact(asset);
  const hoverTitle = art?.url || asset.path;

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
    <div className="group/asset min-w-0">
      <div
        role="button"
        tabIndex={0}
        aria-label={name}
        aria-pressed={selecting ? selected : undefined}
        title={hoverTitle}
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
          ) : art ? (
            <div className="w-full h-full flex flex-col">
              <div className="flex-1 min-h-0 flex flex-col items-center justify-center gap-1.5 px-2">
                <AssetKindIcon asset={asset} />
                <span className="text-[10px] font-semibold tracking-wide text-faint truncate max-w-full">
                  {extBadge(asset)}
                </span>
              </div>
              {/* Bottom strip: last publish + opens-externally badge, then the
                  publisher. The "deleted" badge owns this corner when missing. */}
              {!missing && (
                <div className="shrink-0 px-2 pb-1.5 text-[10px] leading-tight text-faint">
                  <div className="flex items-center gap-1">
                    <span className="flex-1 min-w-0 truncate">
                      {relativeTime(art.last_published_at || asset.last_seen_at)}
                    </span>
                    <ExternalLink className="w-3 h-3 shrink-0" aria-hidden="true" />
                  </div>
                  {art.published_by_agent_name && (
                    <div className="truncate">{art.published_by_agent_name}</div>
                  )}
                </div>
              )}
            </div>
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
            {art ? "deleted" : "missing"}
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

        {/* Top-right: quick star toggle. Unstarred, it shows on hover only and
            not at all on touch (stars there go through select mode / details). */}
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
                : "text-white/90 opacity-0 group-hover:opacity-100 [@media(hover:none)]:hidden"
            }`}
          >
            <Star className="w-4 h-4" fill={asset.starred ? "currentColor" : "none"} strokeWidth={2} />
          </button>
        )}
      </div>

      {/* Caption (two lines; breaks anywhere only if a name part won't fit) +
          details affordance */}
      <div className="flex items-start gap-0.5 mt-1.5 pl-0.5">
        <p className={`min-w-0 flex-1 text-xs leading-snug line-clamp-2 [overflow-wrap:anywhere] ${missing ? "text-faint" : "text-label"}`} title={hoverTitle}>
          {softWrapName(name)}
        </p>
        {!selecting && (
          <button
            type="button"
            onClick={(e) => { e.stopPropagation(); onInfo?.(asset); }}
            title="Details"
            aria-label={`Details for ${name}`}
            className="shrink-0 p-1 -mt-0.5 -mr-1 rounded text-faint hover:text-heading hover:bg-input transition-[opacity,color] [@media(hover:hover)]:opacity-0 group-hover/asset:opacity-100 focus-visible:opacity-100"
          >
            <MoreHorizontal className="w-4 h-4" />
          </button>
        )}
      </div>
    </div>
  );
});

export default AssetTile;
