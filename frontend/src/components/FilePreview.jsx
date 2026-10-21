import { useState, useCallback, useMemo } from "react";
import { authedFetch } from "../lib/api";
import { parseFileUrl, useBatchExists } from "../lib/mediaState";
import { stripAuthParams, withCurrentMediaToken } from "../lib/urls";
import useMediaToken from "../hooks/useMediaToken";
import MissingFileCard from "./MissingFileCard";
import ImageLightbox from "./ImageLightbox";

// useBatchExists now takes URL strings; project this from attachment objects.
function urlsFromAttachments(attachments) {
  return attachments ? attachments.map((a) => a.resolvedUrl) : [];
}

// --- Shared action buttons (download + copy path) ---
//
// Download uses a plain <a download href="…?download=1"> — the backend's
// FileResponse already sets Content-Disposition: attachment, so the browser
// streams to disk and shows progress in its own download manager. No JS Blob,
// no size threshold, no platform branch.

function ActionButtons({ src, filename, originalPath }) {
  const [copied, setCopied] = useState(false);
  const dlHref = src + (src.includes("?") ? "&" : "?") + "download=1";

  const handleCopyPath = (e) => {
    e.stopPropagation();
    e.preventDefault();
    navigator.clipboard.writeText(originalPath || filename).catch(() => {});
    setCopied(true);
    setTimeout(() => setCopied(false), 1500);
  };

  return (
    <span className="inline-flex gap-0.5 shrink-0">
      <a
        href={dlHref}
        download={filename}
        rel="noopener"
        title="Download file"
        onClick={(e) => e.stopPropagation()}
        className="p-0.5 rounded hover:bg-hover transition-colors text-dim hover:text-label"
      >
        <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" strokeWidth={2} viewBox="0 0 24 24">
          <path strokeLinecap="round" strokeLinejoin="round" d="M3 16.5v2.25A2.25 2.25 0 005.25 21h13.5A2.25 2.25 0 0021 18.75V16.5M16.5 12L12 16.5m0 0L7.5 12m4.5 4.5V3" />
        </svg>
      </a>
      <button
        type="button"
        onClick={handleCopyPath}
        title={copied ? "Copied!" : "Copy file path"}
        className="p-0.5 rounded hover:bg-hover transition-colors text-dim hover:text-label"
      >
        {copied ? (
          <svg className="w-3.5 h-3.5 text-ok" fill="none" stroke="currentColor" strokeWidth={2} viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" d="M4.5 12.75l6 6 9-13.5" />
          </svg>
        ) : (
          <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" strokeWidth={2} viewBox="0 0 24 24">
            <rect x="8" y="8" width="12" height="12" rx="2" />
            <path d="M16 8V6a2 2 0 00-2-2H6a2 2 0 00-2 2v8a2 2 0 002 2h2" />
          </svg>
        )}
      </button>
    </span>
  );
}

// --- Image Preview (compact thumbnail, tappable fullscreen) ---

function ImagePreview({ src, thumbSrc, filename, originalPath, exists, onOpen, onRetry }) {
  // Two-stage error: thumb fails → try full-res → then show error UI.
  // Stat says missing → skip the load entirely. Either path shows the
  // shared MissingFileCard (with retry when onRetry provided).
  const [thumbFailed, setThumbFailed] = useState(false);
  const [imgError, setImgError] = useState(false);

  const activeSrc = thumbSrc && !thumbFailed ? thumbSrc : src;

  const handleError = () => {
    if (thumbSrc && !thumbFailed) {
      setThumbFailed(true);
    } else {
      setImgError(true);
    }
  };

  if (exists === false || imgError) {
    return <MissingFileCard filename={filename} originalPath={originalPath} onRetry={onRetry} />;
  }

  return (
    <div>
      <div className="cursor-pointer" onClick={onOpen}>
        <img
          src={activeSrc}
          alt={filename}
          loading="lazy"
          onError={handleError}
          className="chat-attachment-media max-h-[120px] max-w-full rounded-lg border border-divider object-contain"
        />
      </div>
      <div className="flex items-center gap-1 mt-1">
        <p className="text-xs text-dim truncate max-w-[200px]">{filename}</p>
        <ActionButtons src={src} filename={filename} originalPath={originalPath} />
      </div>
    </div>
  );
}

// --- Video Preview (thumbnail, tappable to open in lightbox) ---

function VideoPreview({ src, thumbSrc, filename, originalPath, exists, onOpen, onRetry }) {
  const [thumbError, setThumbError] = useState(false);

  // Stat says missing → skip preview. Otherwise fall back to thumb-404
  // detection (a thumb URL was generated but 404'd → file path didn't
  // resolve; no thumbSrc just means the source isn't on /api/files,
  // e.g. user-uploaded video — that's not a "missing" case).
  if (exists === false || thumbError) {
    return <MissingFileCard filename={filename} originalPath={originalPath} onRetry={onRetry} />;
  }

  return (
    <div>
      <div className="cursor-pointer" onClick={onOpen}>
        <div className="relative inline-block">
          {!thumbSrc ? (
            /* Fallback: gray placeholder when no thumbnail available */
            <div className="w-[160px] h-[90px] rounded-lg border border-divider bg-elevated flex items-center justify-center" />
          ) : (
            <img
              src={thumbSrc}
              alt={filename}
              loading="lazy"
              onError={() => setThumbError(true)}
              className="chat-attachment-media max-h-[120px] max-w-full rounded-lg border border-divider object-contain block"
            />
          )}
          {/* Play icon overlay */}
          <div className="absolute inset-0 flex items-center justify-center rounded-lg">
            <div className="w-8 h-8 rounded-full bg-black/50 flex items-center justify-center">
              <svg className="w-4 h-4 ml-0.5 text-white" fill="currentColor" viewBox="0 0 24 24">
                <path d="M8 5v14l11-7z" />
              </svg>
            </div>
          </div>
        </div>
      </div>
      <div className="flex items-center gap-1 mt-1">
        <p className="text-xs text-dim truncate max-w-[200px]">{filename}</p>
        <ActionButtons src={src} filename={filename} originalPath={originalPath} />
      </div>
    </div>
  );
}

// --- Doc/Code File Preview (collapsible card) ---

function DocFilePreview({ src, filename, ext, originalPath, exists, onRetry }) {
  const [expanded, setExpanded] = useState(false);
  const [content, setContent] = useState(null);
  const [loadState, setLoadState] = useState("idle"); // idle | loading | loaded | error

  const loadContent = useCallback(async () => {
    if (loadState === "loading") return;
    setLoadState("loading");
    try {
      const res = await authedFetch(src);
      if (!res.ok) throw new Error("fetch failed");
      const text = await res.text();
      setContent(text);
      setLoadState("loaded");
    } catch {
      setLoadState("error");
    }
  }, [src, loadState]);

  if (exists === false) return <MissingFileCard filename={filename} originalPath={originalPath} onRetry={onRetry} />;

  const handleToggle = () => {
    if (!expanded && loadState === "idle") loadContent();
    setExpanded((v) => !v);
  };

  const isPdf = ext === "pdf";

  return (
    <div className="rounded-lg bg-elevated overflow-hidden max-w-[280px]">
      <div
        onClick={handleToggle}
        className="w-full flex items-center gap-2 px-3 py-2 hover:bg-hover transition-colors text-left cursor-pointer"
      >
        <svg className="w-4 h-4 text-accent shrink-0" fill="none" stroke="currentColor" strokeWidth={1.5} viewBox="0 0 24 24">
          <path strokeLinecap="round" strokeLinejoin="round" d="M19.5 14.25v-2.625a3.375 3.375 0 00-3.375-3.375h-1.5A1.125 1.125 0 0113.5 7.125v-1.5a3.375 3.375 0 00-3.375-3.375H8.25m2.25 0H5.625c-.621 0-1.125.504-1.125 1.125v17.25c0 .621.504 1.125 1.125 1.125h12.75c.621 0 1.125-.504 1.125-1.125V11.25a9 9 0 00-9-9z" />
        </svg>
        <span className="text-xs text-label truncate flex-1 min-w-0">{filename}</span>
        <span className="text-[10px] text-dim uppercase shrink-0">{ext}</span>
        <ActionButtons src={src} filename={filename} originalPath={originalPath} />
        <svg className={`w-3 h-3 text-dim shrink-0 transition-transform ${expanded ? "rotate-180" : ""}`} fill="none" stroke="currentColor" strokeWidth={2} viewBox="0 0 24 24">
          <path strokeLinecap="round" d="m19 9-7 7-7-7" />
        </svg>
      </div>
      {expanded && (
        <div className="border-t border-divider">
          {loadState === "loading" && (
            <div className="px-3 py-2 text-xs text-dim">Loading...</div>
          )}
          {loadState === "error" && (
            <div className="px-3 py-2 text-xs text-red-400">Failed to load</div>
          )}
          {loadState === "loaded" && !isPdf && content != null && (
            <pre className="px-3 py-2 text-xs text-body font-mono overflow-x-auto max-h-48 whitespace-pre-wrap break-words">
              {content.length > 3000 ? content.slice(0, 3000) + "\n..." : content}
            </pre>
          )}
          {isPdf && (
            <a
              href={src}
              target="_blank"
              rel="noopener noreferrer"
              className="block px-3 py-2 text-xs text-accent hover:underline"
            >
              Open PDF in new tab
            </a>
          )}
        </div>
      )}
    </div>
  );
}

// --- Generic File Card (non-media, non-doc — fallback for user uploads) ---

function GenericFilePreview({ src, filename, originalPath, exists, onRetry }) {
  if (exists === false) return <MissingFileCard filename={filename} originalPath={originalPath} onRetry={onRetry} />;
  return (
    <div className="inline-flex items-center gap-2 px-3 py-2 rounded-lg bg-elevated max-w-[240px]">
      <svg className="w-4 h-4 text-dim shrink-0" fill="none" stroke="currentColor" strokeWidth={1.5} viewBox="0 0 24 24">
        <path strokeLinecap="round" strokeLinejoin="round" d="M19.5 14.25v-2.625a3.375 3.375 0 00-3.375-3.375h-1.5A1.125 1.125 0 0113.5 7.125v-1.5a3.375 3.375 0 00-3.375-3.375H8.25m2.25 0H5.625c-.621 0-1.125.504-1.125 1.125v17.25c0 .621.504 1.125 1.125 1.125h12.75c.621 0 1.125-.504 1.125-1.125V11.25a9 9 0 00-9-9z" />
      </svg>
      <span className="text-xs text-label truncate flex-1 min-w-0">{filename}</span>
      <ActionButtons src={src} filename={filename} originalPath={originalPath} />
    </div>
  );
}

// --- Grouped doc files card (collapsible list for 2+ doc files) ---

function DocGroupRow({ att, exists, onRetry }) {
  const filename = att.path.split("/").pop();
  const missing = exists === false;
  const retryable = missing && !!onRetry;
  const handleClick = (e) => {
    if (!retryable) return;
    e.stopPropagation();
    e.preventDefault();
    onRetry();
  };
  return (
    <div
      className={`flex items-center gap-2 px-3 py-1.5 hover:bg-hover transition-colors text-left ${retryable ? "cursor-pointer" : ""}`}
      title={retryable ? "Tap to check again" : (missing ? att.originalPath || filename : undefined)}
      onClick={retryable ? handleClick : undefined}
    >
      <svg className="w-3.5 h-3.5 text-dim shrink-0" fill="none" stroke="currentColor" strokeWidth={1.5} viewBox="0 0 24 24">
        <path strokeLinecap="round" strokeLinejoin="round" d="M19.5 14.25v-2.625a3.375 3.375 0 00-3.375-3.375h-1.5A1.125 1.125 0 0113.5 7.125v-1.5a3.375 3.375 0 00-3.375-3.375H8.25m2.25 0H5.625c-.621 0-1.125.504-1.125 1.125v17.25c0 .621.504 1.125 1.125 1.125h12.75c.621 0 1.125-.504 1.125-1.125V11.25a9 9 0 00-9-9z" />
      </svg>
      <span className={`text-xs truncate flex-1 min-w-0 ${missing ? "text-dim" : "text-label"}`}>{filename}</span>
      {missing ? (
        <span className="text-[10px] text-dim uppercase shrink-0 opacity-60">missing</span>
      ) : (
        <>
          <span className="text-[10px] text-dim uppercase shrink-0">{att.ext}</span>
          <ActionButtons src={att.resolvedUrl} filename={filename} originalPath={att.originalPath} />
        </>
      )}
    </div>
  );
}

function DocGroupCard({ docs, statMap, onRetry }) {
  const [expanded, setExpanded] = useState(false);

  return (
    <div className="rounded-lg bg-elevated overflow-hidden max-w-[280px]">
      <button
        type="button"
        onClick={() => setExpanded((v) => !v)}
        className="w-full flex items-center gap-2 px-3 py-2 hover:bg-hover transition-colors text-left"
      >
        <svg className="w-4 h-4 text-accent shrink-0" fill="none" stroke="currentColor" strokeWidth={1.5} viewBox="0 0 24 24">
          <path strokeLinecap="round" strokeLinejoin="round" d="M2.25 12.75V12A2.25 2.25 0 014.5 9.75h15A2.25 2.25 0 0121.75 12v.75m-8.69-6.44l-2.12-2.12a1.5 1.5 0 00-1.061-.44H4.5A2.25 2.25 0 002.25 6v12a2.25 2.25 0 002.25 2.25h15A2.25 2.25 0 0021.75 18V9a2.25 2.25 0 00-2.25-2.25h-5.379a1.5 1.5 0 01-1.06-.44z" />
        </svg>
        <span className="text-xs text-label flex-1 min-w-0">{docs.length} files referenced</span>
        <svg className={`w-3 h-3 text-dim shrink-0 transition-transform ${expanded ? "rotate-180" : ""}`} fill="none" stroke="currentColor" strokeWidth={2} viewBox="0 0 24 24">
          <path strokeLinecap="round" d="m19 9-7 7-7-7" />
        </svg>
      </button>
      {expanded && (
        <div className="border-t border-divider max-h-60 overflow-y-auto">
          {docs.map((att) => {
            const stat = statMap[stripAuthParams(att.resolvedUrl)];
            // Non-project URLs (uploads, http) have no stat entry — treat as exists.
            const exists = stat ? stat.exists : true;
            return <DocGroupRow key={att.path} att={att} exists={exists} onRetry={onRetry} />;
          })}
        </div>
      )}
    </div>
  );
}

// --- Main component ---

export default function FileAttachments({ attachments: given, compact }) {
  // Callers memoize attachments (ChatBubble), so their URLs keep the media
  // token of their first render. Re-stamp them with the current token, and
  // re-render when AuthGuard re-mints it, so thumbnails, downloads and the
  // lightbox outlive the old token.
  const mediaToken = useMediaToken();
  const attachments = useMemo(() => given && given.map((a) => ({
    ...a,
    resolvedUrl: withCurrentMediaToken(a.resolvedUrl, mediaToken),
    thumbUrl: withCurrentMediaToken(a.thumbUrl, mediaToken),
  })), [given, mediaToken]);
  const [lightbox, setLightbox] = useState(null); // { media, initialIndex } or null
  const urls = useMemo(() => urlsFromAttachments(attachments), [attachments]);
  const { statMap, refresh: refreshStat } = useBatchExists(urls);

  if (!attachments || attachments.length === 0) return null;
  // Per-att helper: resolves the batched stat (or treats non-project URLs as exists).
  const statFor = (att) => statMap[stripAuthParams(att.resolvedUrl)] || (parseFileUrl(att.resolvedUrl) ? null : { exists: true });

  // Split into media (inline) vs doc/file (groupable)
  const mediaAtts = [];
  const docs = [];
  const other = [];
  for (const att of attachments) {
    if (att.type === "image" || att.type === "video") mediaAtts.push(att);
    else if (att.type === "doc") docs.push(att);
    else other.push(att);
  }

  // Unified media gallery: images and videos in one swipeable lightbox
  const allAtts = [...mediaAtts, ...docs, ...other];
  const galleryMedia = mediaAtts.map((att) => ({
    type: att.type,
    src: att.resolvedUrl,
    thumbSrc: att.thumbUrl,
    filename: att.path.split("/").pop(),
  }));

  const openLightbox = (mediaIndex) => {
    setLightbox({ media: galleryMedia, initialIndex: mediaIndex });
  };

  // Compact pill layout — like the compose bar attachment chips
  if (compact) {
    return (
      <div className="mt-1.5">
        <div className="flex flex-wrap gap-1.5 justify-end">
          {allAtts.map((att, idx) => {
            const filename = att.path.split("/").pop();
            const isMedia = att.type === "image" || att.type === "video";
            const mediaIdx = isMedia ? mediaAtts.indexOf(att) : -1;
            return (
              <div
                key={att.path}
                className="flex items-center gap-1.5 px-2 py-1 rounded-lg bg-elevated text-xs max-w-[180px] cursor-pointer"
                onClick={() => isMedia ? openLightbox(mediaIdx) : null}
              >
                {isMedia ? (
                  <img src={att.thumbUrl || att.resolvedUrl} alt="" className="chat-attachment-media w-8 h-8 rounded object-cover shrink-0" />
                ) : (
                  <svg className="w-4 h-4 text-dim shrink-0" fill="none" stroke="currentColor" strokeWidth={1.5} viewBox="0 0 24 24">
                    <path strokeLinecap="round" strokeLinejoin="round" d="M19.5 14.25v-2.625a3.375 3.375 0 00-3.375-3.375h-1.5A1.125 1.125 0 0113.5 7.125v-1.5a3.375 3.375 0 00-3.375-3.375H8.25m2.25 0H5.625c-.621 0-1.125.504-1.125 1.125v17.25c0 .621.504 1.125 1.125 1.125h12.75c.621 0 1.125-.504 1.125-1.125V11.25a9 9 0 00-9-9z" />
                  </svg>
                )}
                <span className="truncate text-label flex-1 min-w-0">{filename}</span>
              </div>
            );
          })}
        </div>
        {lightbox && (
          <ImageLightbox
            media={lightbox.media}
            initialIndex={lightbox.initialIndex}
            onClose={() => setLightbox(null)}
          />
        )}
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-2 mt-1.5">
      {/* Images and videos always render inline */}
      {mediaAtts.map((att, idx) => {
        const filename = att.path.split("/").pop();
        const stat = statFor(att);
        const exists = stat ? stat.exists : null;
        if (att.type === "image") {
          return (
            <ImagePreview
              key={att.path}
              src={att.resolvedUrl}
              thumbSrc={att.thumbUrl}
              filename={filename}
              originalPath={att.originalPath}
              exists={exists}
              onOpen={() => openLightbox(idx)}
              onRetry={refreshStat}
            />
          );
        }
        return (
          <VideoPreview
            key={att.path}
            src={att.resolvedUrl}
            thumbSrc={att.thumbUrl}
            filename={filename}
            originalPath={att.originalPath}
            exists={exists}
            onOpen={() => openLightbox(idx)}
            onRetry={refreshStat}
          />
        );
      })}
      {/* Doc files: single card if 1, grouped card if 2+ */}
      {docs.length === 1 && (() => {
        const stat = statFor(docs[0]);
        return (
          <DocFilePreview
            src={docs[0].resolvedUrl}
            filename={docs[0].path.split("/").pop()}
            ext={docs[0].ext}
            originalPath={docs[0].originalPath}
            exists={stat ? stat.exists : null}
            onRetry={refreshStat}
          />
        );
      })()}
      {docs.length >= 2 && <DocGroupCard docs={docs} statMap={statMap} onRetry={refreshStat} />}
      {/* Generic fallback for non-media, non-doc */}
      {other.map((att) => {
        const stat = statFor(att);
        return (
          <GenericFilePreview
            key={att.path}
            src={att.resolvedUrl}
            filename={att.path.split("/").pop()}
            originalPath={att.originalPath}
            exists={stat ? stat.exists : null}
            onRetry={refreshStat}
          />
        );
      })}

      {/* Lightbox for media gallery */}
      {lightbox && (
        <ImageLightbox
          media={lightbox.media}
          initialIndex={lightbox.initialIndex}
          onClose={() => setLightbox(null)}
        />
      )}

    </div>
  );
}
