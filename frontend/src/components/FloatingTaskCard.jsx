import { useState, useEffect, useRef, useMemo } from "react";
import { useNavigate, useLocation } from "react-router-dom";
import { fetchTaskV2, updateTaskV2 } from "../lib/api";
import { renderMarkdown, relativeTime } from "../lib/formatters";
import { modelDisplayName } from "../lib/constants";
import { uploadUrl } from "../lib/urls";
import ImageLightbox from "./ImageLightbox";
import useDraft from "../hooks/useDraft";
import { forwardState } from "../lib/nav";

const STATUS_DOT = {
  inbox: "bg-slate-400",
  pending: "bg-blue-500",
  executing: "bg-amber-500",
  complete: "bg-ok",
  failed: "bg-red-500",
  cancelled: "bg-gray-400",
  timeout: "bg-orange-500",
};

const STATUS_LABEL = {
  inbox: "Inbox",
  pending: "Pending",
  executing: "In Progress",
  complete: "Complete",
  failed: "Failed",
  cancelled: "Dropped",
  timeout: "Timeout",
  review: "Review",
  merging: "Merging",
  conflict: "Conflict",
  planning: "Planning",
  rejected: "Rejected",
};

const ATTACH_RE = /\[Attached file: ([^\]]+)\]/g;
function parseDesc(desc) {
  if (!desc) return { text: "", files: [] };
  const files = [];
  let m;
  while ((m = ATTACH_RE.exec(desc)) !== null) files.push(m[1]);
  ATTACH_RE.lastIndex = 0;
  const text = desc.replace(ATTACH_RE, "").replace(/\n{2,}/g, "\n").trim();
  return { text, files };
}
function fileName(p) { return p.split("/").pop() || p; }
function isImagePath(p) { return /\.(png|jpe?g|gif|webp|svg|bmp|ico)$/i.test(p); }

export default function FloatingTaskCard({ taskId, onClose, onAction }) {
  const navigate = useNavigate();
  const location = useLocation();
  const [task, setTask] = useState(null);
  const [loading, setLoading] = useState(true);
  const [editingTitle, setEditingTitle] = useState(false);
  const [editingDesc, setEditingDesc] = useState(false);
  const [selectedPill, setSelectedPill] = useState(null);
  const [editingNote, setEditingNote] = useState(false);
  const [lightboxIndex, setLightboxIndex] = useState(null);
  const [titleDraft, setTitleDraft, clearTitleDraft] = useDraft(taskId ? `task-edit:${taskId}:title` : null);
  const [descDraft, setDescDraft, clearDescDraft] = useDraft(taskId ? `task-edit:${taskId}:desc` : null);
  const [noteDraft, setNoteDraft, clearNoteDraft] = useDraft(taskId ? `task-edit:${taskId}:note` : null);
  const cardRef = useRef(null);
  const parsed = useMemo(() => parseDesc(task?.description), [task?.description]);

  useEffect(() => {
    if (!taskId) return;
    setLoading(true);
    fetchTaskV2(taskId)
      .then((t) => {
        setTask(t);
        if (t.attempt_agents?.length) setSelectedPill(t.attempt_agents.length - 1);
        if (localStorage.getItem(`draft:task-edit:${taskId}:title`) === null) setTitleDraft(t.title || "");
        if (localStorage.getItem(`draft:task-edit:${taskId}:desc`) === null) setDescDraft(t.description || "");
        if (localStorage.getItem(`draft:task-edit:${taskId}:note`) === null) setNoteDraft(t.note || "");
      })
      .catch((e) => console.warn("Task fetch failed:", e))
      .finally(() => setLoading(false));
  }, [taskId]);

  useEffect(() => {
    const handler = (e) => {
      if (cardRef.current && !cardRef.current.contains(e.target)) onClose();
    };
    document.addEventListener("mousedown", handler);
    return () => document.removeEventListener("mousedown", handler);
  }, [onClose]);

  const saveTitle = async () => {
    if (!task || titleDraft.trim() === task.title) { setEditingTitle(false); return; }
    try {
      const updated = await updateTaskV2(task.id, { title: titleDraft.trim() });
      setTask(updated);
      clearTitleDraft();
    } catch (e) { console.warn("Task update failed:", e); }
    setEditingTitle(false);
  };

  const saveDesc = async () => {
    if (!task || descDraft.trim() === (task.description || "")) { setEditingDesc(false); return; }
    try {
      const updated = await updateTaskV2(task.id, { description: descDraft.trim() });
      setTask(updated);
      clearDescDraft();
    } catch (e) { console.warn("Task update failed:", e); }
    setEditingDesc(false);
  };

  const saveNote = async () => {
    if (!task || noteDraft.trim() === (task.note || "")) { setEditingNote(false); return; }
    try {
      const updated = await updateTaskV2(task.id, { note: noteDraft.trim() || null });
      setTask(updated);
      clearNoteDraft();
    } catch (e) { console.warn("Note update failed:", e); }
    setEditingNote(false);
  };

  if (!taskId) return null;

  const canEdit = task?.status === "inbox";
  const statusKey = task?.status?.toLowerCase();

  return (
    <div className="fixed inset-0 z-40 flex items-center justify-center bg-black/40 p-4">
      <div ref={cardRef} className="bg-surface rounded-2xl shadow-[0_8px_30px_rgba(0,0,0,0.12)] dark:shadow-[0_8px_30px_rgba(0,0,0,0.4)] border border-divider max-w-md w-full max-h-[80vh] overflow-y-auto">

        {/* ── Header: close + title ── */}
        <div className="px-5 pt-5 pb-3">
          <div className="flex items-start justify-between gap-3">
            <div className="flex-1 min-w-0">
              {editingTitle ? (
                <input
                  autoComplete="off"
                  autoFocus
                  value={titleDraft}
                  onChange={(e) => setTitleDraft(e.target.value)}
                  onBlur={saveTitle}
                  onKeyDown={(e) => { if (e.key === "Enter") saveTitle(); if (e.key === "Escape") { setTitleDraft(task?.title || ""); setEditingTitle(false); } }}
                  className="w-full text-lg font-semibold text-heading bg-transparent px-0 py-0 border-0 border-b border-accent focus:outline-none"
                />
              ) : (
                <h3
                  onClick={() => { if (canEdit) setEditingTitle(true); }}
                  className={`text-lg font-semibold text-heading leading-snug ${canEdit ? "cursor-pointer hover:text-accent" : ""}`}
                >
                  {task?.title || "Untitled"}
                </h3>
              )}
            </div>
            <button type="button" onClick={onClose}
              className="shrink-0 w-7 h-7 flex items-center justify-center rounded-full text-faint hover:text-body hover:bg-input transition-colors -mt-0.5">
              <svg className="w-4 h-4" fill="none" stroke="currentColor" strokeWidth={2} viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" d="M6 18L18 6M6 6l12 12" />
              </svg>
            </button>
          </div>
        </div>

        {loading ? (
          <div className="px-5 pb-5 text-center text-dim text-sm">Loading...</div>
        ) : task ? (
          <>
            {/* ── Tags (matches InboxCard style) ── */}
            <div className="px-5 flex flex-wrap items-center gap-1.5">
              {/* Status dot + label */}
              <span className="text-[10px] font-medium px-1.5 py-px rounded-full bg-elevated text-dim inline-flex items-center gap-1">
                <span className={`w-1.5 h-1.5 rounded-full ${STATUS_DOT[statusKey] || "bg-gray-400"} ${statusKey === "executing" ? "animate-pulse" : ""}`} />
                {STATUS_LABEL[statusKey] || task.status}
              </span>
              {/* Project */}
              {task.project_name && (
                <span className="text-[10px] font-medium px-1.5 py-px rounded-full accent-tint-15 text-accent">
                  {task.project_name}
                </span>
              )}
              {/* Worktree */}
              {task.use_worktree !== false && (
                <span className="text-[10px] font-medium px-1.5 py-px rounded-full bg-purple-500/15 text-purple-500 dark:text-purple-400 inline-flex items-center gap-0.5">
                  <svg className="w-3 h-3" fill="none" stroke="currentColor" strokeWidth={2} viewBox="0 0 24 24">
                    <path strokeLinecap="round" strokeLinejoin="round" d="M6 3v12M18 9a3 3 0 100-6 3 3 0 000 6zm0 0v3a3 3 0 01-3 3H9m-3 0a3 3 0 100 6 3 3 0 000-6z" />
                  </svg>
                  {task.worktree_name || "Worktree"}
                </span>
              )}
              {/* Auto */}
              {task.skip_permissions && (
                <span className="text-[10px] font-medium px-1.5 py-px rounded-full bg-amber-500/15 text-amber-500 dark:text-amber-400">
                  Auto
                </span>
              )}
              {/* Model */}
              {task.model && (
                <span className="text-[10px] font-medium px-1.5 py-px rounded-full bg-elevated text-dim">
                  {modelDisplayName(task.model)}
                </span>
              )}
              {/* Effort */}
              {task.effort && (
                <span className="text-[10px] font-medium px-1.5 py-px rounded-full bg-elevated text-dim">
                  {task.effort.charAt(0).toUpperCase() + task.effort.slice(1)}
                </span>
              )}
              {/* Retry */}
              {task.attempt_number > 1 && (
                <span className="text-[10px] font-semibold px-1.5 py-px rounded-full bg-orange-500/15 text-orange-500 dark:text-orange-400">
                  Retry #{task.attempt_number}
                </span>
              )}
            </div>

            {/* ── Description section ── */}
            <div className="px-5 pt-4">
              <p className="text-[11px] font-medium text-dim uppercase tracking-wider mb-2">Description</p>
              {editingDesc ? (
                <textarea
                  autoFocus
                  value={descDraft}
                  onChange={(e) => setDescDraft(e.target.value)}
                  onBlur={saveDesc}
                  rows={4}
                  className="w-full text-sm text-body bg-transparent px-0 py-0 resize-none focus:outline-none border-0 placeholder-hint"
                  placeholder="Add description..."
                />
              ) : (
                <div
                  onClick={() => { if (canEdit) setEditingDesc(true); }}
                  className={canEdit ? "cursor-pointer" : ""}
                >
                  {parsed.text ? (
                    <p className="text-sm text-body leading-relaxed whitespace-pre-wrap">{parsed.text}</p>
                  ) : !parsed.files.length ? (
                    <p className="text-sm text-hint">Add description</p>
                  ) : null}
                </div>
              )}
              {/* Attachment thumbnails */}
              {!editingDesc && parsed.files.length > 0 && (
                <div className="flex flex-wrap gap-2 mt-2">
                  {parsed.files.map((f, i) => {
                    const name = fileName(f);
                    const isImg = isImagePath(f);
                    const src = uploadUrl(name);
                    return (
                      <div
                        key={f}
                        className="flex items-center gap-1.5 px-2 py-1 rounded-lg bg-elevated text-xs max-w-[180px] cursor-pointer hover:bg-hover transition-colors"
                        onClick={(e) => { e.stopPropagation(); if (isImg) setLightboxIndex(i); }}
                      >
                        {isImg ? (
                          <img src={src} alt="" className="w-8 h-8 rounded object-cover shrink-0" />
                        ) : (
                          <svg className="w-4 h-4 text-dim shrink-0" fill="none" stroke="currentColor" strokeWidth={1.5} viewBox="0 0 24 24">
                            <path strokeLinecap="round" strokeLinejoin="round" d="M19.5 14.25v-2.625a3.375 3.375 0 00-3.375-3.375h-1.5A1.125 1.125 0 0113.5 7.125v-1.5a3.375 3.375 0 00-3.375-3.375H8.25m2.25 0H5.625c-.621 0-1.125.504-1.125 1.125v17.25c0 .621.504 1.125 1.125 1.125h12.75c.621 0 1.125-.504 1.125-1.125V11.25a9 9 0 00-9-9z" />
                          </svg>
                        )}
                        <span className="truncate flex-1 min-w-0 text-dim">{name}</span>
                      </div>
                    );
                  })}
                </div>
              )}
            </div>

            {/* ── Attempts section ── */}
            {task.attempt_agents?.length > 0 && (() => {
              const total = task.attempt_agents.length;
              const sel = selectedPill ?? total - 1;
              const showSummary = sel < total - 1 && sel === total - 2 && task.agent_summary;
              const showUserFeedback = sel < total - 1 && sel === total - 2 && task.retry_context;

              return (
                <div className="px-5 pt-4">
                  <p className="text-[11px] font-medium text-dim uppercase tracking-wider mb-2">Attempts</p>
                  <div className="flex items-center gap-1.5 flex-wrap">
                    {task.attempt_agents.map((a, i) => (
                      <button
                        key={a.agent_id}
                        type="button"
                        onClick={() => setSelectedPill(i)}
                        className={`px-2.5 py-1 rounded-full text-[11px] font-medium transition-colors ${
                          i === sel
                            ? "bg-accent text-white"
                            : "bg-input text-dim hover:text-body"
                        }`}
                      >
                        #{i + 1}
                      </button>
                    ))}
                    <button
                      type="button"
                      onClick={() => { onClose(); navigate(`/agents/${task.attempt_agents[sel].agent_id}`, { state: forwardState(location) }); }}
                      className="ml-auto text-[11px] font-medium text-accent hover:opacity-80 transition-colors"
                    >
                      Enter Chat &rarr;
                    </button>
                  </div>

                  {showSummary && (
                    <div className="mt-3">
                      <p className="text-[11px] font-medium text-dim mb-1">Agent Summary</p>
                      {task.agent_summary === ":::generating:::" ? (
                        <p className="text-xs text-faint italic">Generating summary...</p>
                      ) : (
                        <p className="text-xs text-body leading-relaxed whitespace-pre-wrap">{task.agent_summary}</p>
                      )}
                    </div>
                  )}

                  {showUserFeedback && (
                    <div className="mt-3">
                      <p className="text-[11px] font-medium text-dim mb-1">User Feedback</p>
                      <p className="text-xs text-body leading-relaxed whitespace-pre-wrap">{task.retry_context}</p>
                    </div>
                  )}
                </div>
              );
            })()}

            {/* ── Quick Note — personal memo, not sent to model ── */}
            <div className="px-5 pt-4">
              <p className="text-[11px] font-medium text-dim uppercase tracking-wider mb-2">Quick Note</p>
              {editingNote || !task.note ? (
                <textarea
                  autoFocus={editingNote}
                  value={noteDraft}
                  onChange={(e) => setNoteDraft(e.target.value)}
                  onFocus={() => setEditingNote(true)}
                  onBlur={saveNote}
                  onKeyDown={(e) => { if (e.key === "Escape") { setNoteDraft(task.note || ""); setEditingNote(false); } }}
                  rows={2}
                  className="w-full text-sm text-body bg-transparent px-0 py-0 resize-none focus:outline-none border-0 placeholder-hint"
                  placeholder="Jot something down..."
                />
              ) : (
                <div
                  onClick={() => { setNoteDraft(task.note || ""); setEditingNote(true); }}
                  className="cursor-pointer"
                >
                  <div className="text-sm text-body leading-relaxed prose-sm">{renderMarkdown(task.note, task.project_name)}</div>
                </div>
              )}
            </div>

            {/* ── Actions ── */}
            {task.status === "executing" && onAction && (
              <div className="px-5 pt-4 flex gap-2">
                <button
                  type="button"
                  onClick={() => onAction("complete")}
                  className="flex-1 px-3 py-2 rounded-lg text-xs font-semibold bg-green-600 text-white hover:bg-green-500 transition-colors"
                >
                  Complete
                </button>
                <button
                  type="button"
                  onClick={() => onAction("incomplete")}
                  className="flex-1 px-3 py-2 rounded-lg text-xs font-semibold bg-transparent border border-edge text-dim hover:text-body hover:border-ring-hover transition-colors"
                >
                  Mark Incomplete
                </button>
              </div>
            )}

            {/* Bottom spacing */}
            <div className="h-5" />

            {/* Image lightbox */}
            {lightboxIndex != null && (() => {
              const imageFiles = parsed.files.filter(isImagePath);
              const media = imageFiles.map((f) => ({
                type: "image",
                src: uploadUrl(fileName(f)),
                filename: fileName(f),
              }));
              // Map clicked index in parsed.files to index in imageFiles
              const imgIdx = imageFiles.indexOf(parsed.files[lightboxIndex]);
              return media.length > 0 ? (
                <ImageLightbox
                  media={media}
                  initialIndex={imgIdx >= 0 ? imgIdx : 0}
                  onClose={() => setLightboxIndex(null)}
                />
              ) : null;
            })()}
          </>
        ) : (
          <div className="px-5 pb-5 text-center text-dim text-sm">Task not found</div>
        )}
      </div>
    </div>
  );
}
