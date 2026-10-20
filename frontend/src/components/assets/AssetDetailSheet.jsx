import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { Check, Copy, Download, Eye, EyeOff, MessageSquare, Star, X } from "lucide-react";
import { relativeTime } from "../../lib/formatters";
import { formatBytes } from "../dropbox/format";
import { AssetKindIcon } from "./AssetTile";
import { assetWebapp, displayName, extBadge, parseTs } from "./assetFilters";
import { chatLinkFor, isFileAsset, openActionFor } from "./assetMedia";

const SOURCE_LABELS = {
  text: "Mentioned",
  write: "Written",
  send: "Sent",
  webapp: "Web app",
  read: "Read",
};

const KIND_LABELS = {
  image: "Image",
  video: "Video",
  html: "HTML",
  pdf: "PDF",
  office: "Office",
  "3d": "3D",
  webapp: "Web app",
};

const OPEN_LABELS = {
  lightbox: "View",
  static: "Open preview",
  port: "Open app",
  "port-chat": "Open from chat",
  url: "Open link",
  pdf: "View PDF",
  download: "Download",
};

function fmtAbs(ms) {
  if (!ms) return "";
  return new Date(ms).toLocaleString(undefined, {
    year: "numeric", month: "short", day: "numeric", hour: "2-digit", minute: "2-digit",
  });
}

function Row({ label, children, title }) {
  return (
    <div className="flex items-baseline justify-between gap-3 py-1.5 border-b border-divider last:border-b-0">
      <dt className="text-xs text-dim shrink-0">{label}</dt>
      <dd className="text-xs text-body text-right min-w-0 truncate" title={title}>{children}</dd>
    </div>
  );
}

/**
 * Bottom sheet with an asset's metadata, marks (star / hide / note) and
 * actions (open, jump to chat, download). `asset` is the live object from
 * AssetsSection state, so mark changes from any source re-render here.
 */
export default function AssetDetailSheet({ asset, onClose, onOpen, onMark, onOpenChat, onDownload }) {
  const [entered, setEntered] = useState(false);
  const [copied, setCopied] = useState(false);
  const [noteDraft, setNoteDraft] = useState(asset?.note || "");
  const [savingNote, setSavingNote] = useState(false);
  const copyTimer = useRef(null);
  const path = asset?.path;

  // Re-seed the note draft when switching to another asset, or when the note
  // changes elsewhere while the user hasn't touched the field.
  const lastSeenNote = useRef(asset?.note || "");
  useEffect(() => {
    const incoming = asset?.note || "";
    // Capture before advancing the ref — the updater runs lazily.
    const prevSeen = lastSeenNote.current;
    lastSeenNote.current = incoming;
    setNoteDraft((cur) => (cur === prevSeen ? incoming : cur));
  }, [asset?.note]);
  useEffect(() => {
    setNoteDraft(asset?.note || "");
    lastSeenNote.current = asset?.note || "";
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [path]);

  useEffect(() => {
    const raf = requestAnimationFrame(() => setEntered(true));
    return () => cancelAnimationFrame(raf);
  }, []);

  useEffect(() => {
    const onKey = (e) => { if (e.key === "Escape") onClose?.(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  // Block iOS momentum-scroll bleed onto the page behind (ProjectBrowserModal pattern).
  useEffect(() => {
    const blockBg = (e) => {
      if (e.target?.closest?.("[data-overlay]")) return;
      e.preventDefault();
    };
    document.addEventListener("touchmove", blockBg, { passive: false });
    return () => document.removeEventListener("touchmove", blockBg);
  }, []);

  useEffect(() => () => clearTimeout(copyTimer.current), []);

  if (!asset) return null;

  const action = openActionFor(asset);
  const chatLink = chatLinkFor(asset);
  const isFile = isFileAsset(asset);
  const missing = asset.exists === false;
  const app = assetWebapp(asset);
  const name = displayName(asset);
  const badge = extBadge(asset);
  const firstMs = parseTs(asset.first_seen_at);
  const lastMs = parseTs(asset.last_seen_at);
  const noteDirty = noteDraft.trim() !== (asset.note || "").trim();

  const copyPath = () => {
    const text = app?.kind === "url" || app?.kind === "port" ? (app.target || asset.path) : asset.path;
    navigator.clipboard?.writeText(text).catch(() => {});
    setCopied(true);
    clearTimeout(copyTimer.current);
    copyTimer.current = setTimeout(() => setCopied(false), 1500);
  };

  const saveNote = async () => {
    if (!noteDirty || savingNote) return;
    setSavingNote(true);
    try {
      await onMark?.([asset.path], { note: noteDraft.trim() });
    } finally {
      setSavingNote(false);
    }
  };

  return createPortal(
    <div data-overlay className="fixed inset-0 z-50 flex flex-col justify-end sm:justify-center items-center">
      <div
        className="absolute inset-0 bg-black/40 transition-opacity duration-200"
        style={{ opacity: entered ? 1 : 0, touchAction: "none" }}
        onClick={onClose}
      />
      <div
        role="dialog"
        aria-modal="true"
        aria-label={`Asset details: ${name}`}
        className="relative z-10 w-full sm:max-w-md bg-page rounded-t-[20px] sm:rounded-2xl shadow-2xl flex flex-col max-h-[85vh] transition-transform duration-300 ease-[cubic-bezier(0.32,0.72,0,1)]"
        style={{ transform: entered ? "translateY(0)" : "translateY(24px)", paddingBottom: "env(safe-area-inset-bottom, 0px)" }}
      >
        {/* Header */}
        <div className="shrink-0 flex items-start gap-3 px-5 pt-4 pb-3 border-b border-divider">
          <div className="shrink-0 w-10 h-10 rounded-xl bg-surface flex items-center justify-center">
            <AssetKindIcon asset={asset} className="w-5 h-5" />
          </div>
          <div className="min-w-0 flex-1">
            <h3 className="text-base font-semibold text-heading break-words leading-snug">{name}</h3>
            <div className="flex flex-wrap items-center gap-1 mt-1">
              <span className="text-[10px] font-medium px-1.5 py-px rounded-full bg-elevated text-label">
                {KIND_LABELS[asset.kind] || asset.kind}
              </span>
              {badge && badge !== (KIND_LABELS[asset.kind] || "").toUpperCase() && (
                <span className="text-[10px] font-medium px-1.5 py-px rounded-full bg-elevated text-label">{badge}</span>
              )}
              {asset.tier === "seen" && (
                <span className="text-[10px] font-medium px-1.5 py-px rounded-full bg-elevated text-dim">Seen by agent</span>
              )}
              {missing && (
                <span className="text-[10px] font-semibold px-1.5 py-px rounded-full danger-tint-15 text-danger">
                  {app?.kind === "port" ? "Not running" : "Missing"}
                </span>
              )}
            </div>
          </div>
          <button
            type="button"
            onClick={onClose}
            aria-label="Close"
            className="shrink-0 w-8 h-8 -mr-2 -mt-1 flex items-center justify-center rounded-lg text-label hover:text-heading hover:bg-input transition-colors"
          >
            <X className="w-5 h-5" />
          </button>
        </div>

        {/* Body */}
        <div className="flex-1 overflow-y-auto px-5 py-3 space-y-4" style={{ overscrollBehavior: "contain" }}>
          {/* Path */}
          <div className="flex items-start gap-2 rounded-xl bg-surface px-3 py-2">
            <code className="text-xs font-mono text-body break-all flex-1 min-w-0">
              {app?.kind === "url" || app?.kind === "port" ? app.target : asset.path}
            </code>
            <button
              type="button"
              onClick={copyPath}
              title={copied ? "Copied!" : "Copy path"}
              aria-label="Copy path"
              className="shrink-0 p-1 -m-0.5 rounded text-dim hover:text-label hover:bg-input transition-colors"
            >
              {copied ? <Check className="w-3.5 h-3.5 text-ok" /> : <Copy className="w-3.5 h-3.5" />}
            </button>
          </div>

          {/* Marks */}
          <div className="grid grid-cols-2 gap-2">
            <button
              type="button"
              onClick={() => onMark?.([asset.path], { starred: !asset.starred }, { sticky: true })}
              className={`flex items-center justify-center gap-2 min-h-[40px] rounded-lg text-sm font-medium transition-colors ${
                asset.starred
                  ? "bg-amber-500/15 text-amber-600 dark:text-amber-400 hover:bg-amber-500/25"
                  : "bg-surface text-label hover:bg-input"
              }`}
            >
              <Star className="w-4 h-4" fill={asset.starred ? "currentColor" : "none"} />
              {asset.starred ? "Starred" : "Star"}
            </button>
            <button
              type="button"
              onClick={() => onMark?.([asset.path], { hidden: !asset.hidden }, { sticky: true })}
              className={`flex items-center justify-center gap-2 min-h-[40px] rounded-lg text-sm font-medium transition-colors ${
                asset.hidden ? "bg-elevated text-heading hover:bg-hover" : "bg-surface text-label hover:bg-input"
              }`}
            >
              {asset.hidden ? <EyeOff className="w-4 h-4" /> : <Eye className="w-4 h-4" />}
              {asset.hidden ? "Hidden" : "Hide"}
            </button>
          </div>

          {/* Note */}
          <div>
            <label htmlFor="asset-note" className="block text-xs font-medium text-dim mb-1">Note</label>
            <textarea
              id="asset-note"
              value={noteDraft}
              onChange={(e) => setNoteDraft(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) { e.preventDefault(); saveNote(); }
              }}
              rows={2}
              placeholder="Add a note (⌘+Enter saves)"
              disabled={savingNote}
              className="w-full rounded-lg bg-surface border border-divider px-3 py-2 text-sm text-body placeholder-hint resize-none focus:outline-none focus:ring-1 focus:ring-accent disabled:opacity-60"
            />
            {noteDirty && (
              <div className="flex justify-end gap-2 mt-1.5">
                <button
                  type="button"
                  onClick={() => setNoteDraft(asset.note || "")}
                  disabled={savingNote}
                  className="px-3 py-1 rounded-lg text-xs text-dim hover:text-body transition-colors"
                >
                  Revert
                </button>
                <button
                  type="button"
                  onClick={saveNote}
                  disabled={savingNote}
                  className="px-3 py-1 rounded-lg bg-accent text-accent-ink text-xs font-medium hover:opacity-90 disabled:opacity-50 transition-opacity"
                >
                  {savingNote ? "Saving…" : noteDraft.trim() ? "Save note" : "Clear note"}
                </button>
              </div>
            )}
          </div>

          {/* Metadata */}
          <dl>
            {isFile && <Row label="Size">{asset.size != null ? formatBytes(asset.size) : "—"}</Row>}
            {isFile && asset.mtime != null && <Row label="Modified">{fmtAbs(asset.mtime * 1000)}</Row>}
            {firstMs > 0 && <Row label="First seen" title={fmtAbs(firstMs)}>{relativeTime(asset.first_seen_at)}</Row>}
            {lastMs > 0 && <Row label="Last seen" title={fmtAbs(lastMs)}>{relativeTime(asset.last_seen_at)}</Row>}
            {asset.mention_count > 0 && <Row label="References">{asset.mention_count}</Row>}
            {(asset.last_agent_name || asset.last_agent_id) && (
              <Row label="Last agent">{asset.last_agent_name || asset.last_agent_id}</Row>
            )}
            {Array.isArray(asset.sources) && asset.sources.length > 0 && (
              <Row label="Sources">{asset.sources.map((s) => SOURCE_LABELS[s] || s).join(" · ")}</Row>
            )}
          </dl>
        </div>

        {/* Actions */}
        <div className="shrink-0 flex items-center gap-2 px-5 pt-3 pb-4 border-t border-divider">
          {action !== "details" && action !== "download" && (
            <button
              type="button"
              onClick={() => onOpen?.(asset)}
              className="flex-1 min-h-[40px] rounded-lg bg-accent text-accent-ink text-sm font-semibold hover:opacity-90 transition-opacity"
            >
              {OPEN_LABELS[action] || "Open"}
            </button>
          )}
          {chatLink && (
            <button
              type="button"
              onClick={() => onOpenChat?.(asset)}
              className="flex-1 flex items-center justify-center gap-1.5 min-h-[40px] rounded-lg bg-surface text-body text-sm font-medium hover:bg-input transition-colors"
            >
              <MessageSquare className="w-4 h-4" />
              Open in chat
            </button>
          )}
          {isFile && !missing && (
            <button
              type="button"
              onClick={() => onDownload?.(asset)}
              title="Download"
              aria-label="Download"
              className={`flex items-center justify-center gap-1.5 min-h-[40px] rounded-lg bg-surface text-body text-sm font-medium hover:bg-input transition-colors ${
                action === "download" ? "flex-1" : "w-11 shrink-0"
              }`}
            >
              <Download className="w-4 h-4" />
              {action === "download" && "Download"}
            </button>
          )}
        </div>
      </div>
    </div>,
    document.body,
  );
}
