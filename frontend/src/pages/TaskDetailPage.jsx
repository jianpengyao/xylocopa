import { useState, useEffect, useCallback, useRef } from "react";
import { Bell } from "lucide-react";
import { useParams, useNavigate, useLocation } from "react-router-dom";
import { fetchTaskV2, updateTaskV2, dispatchTask, cancelTask } from "../lib/api";
import { TASK_STATUS_COLORS, TASK_STATUS_TEXT_COLORS, projectBadgeColor, modelDisplayName, POLL_INTERVAL } from "../lib/constants";
import { relativeTime, renderMarkdown, durationDisplay, elapsedDisplay, toLocalInputValue, DATE_SHORT } from "../lib/formatters";
import { serverNow } from "../lib/serverTime";
import ProjectSelector from "../components/ProjectSelector";
import SendLaterPicker from "../components/SendLaterPicker";
import usePageVisible from "../hooks/usePageVisible";
import useWebSocket, { useWsEvent, registerViewingTasks, unregisterViewingTasks } from "../hooks/useWebSocket";
import { useToast } from "../contexts/ToastContext";
import useDraft from "../hooks/useDraft";
import { forwardState } from "../lib/nav";
import { taskDetailCache, taskBriefCache } from "../lib/detailCache";
import TaskDetailSkeleton from "../components/skeletons/TaskDetailSkeleton";

export default function TaskDetailPage({ theme, onToggleTheme }) {
  const { id } = useParams();
  const navigate = useNavigate();
  const location = useLocation();
  // Seed from full-detail cache first, fall back to brief cache so a
  // never-visited task still paints its header (title / status / project)
  // from the TasksPage list snapshot before fetchTaskV2 returns.
  const initialCached = taskDetailCache.get(id);
  const initialBrief = initialCached?.task || taskBriefCache.get(id) || null;
  const [task, setTask] = useState(initialBrief);
  const [loading, setLoading] = useState(!initialCached);
  const [error, setError] = useState(null);
  const [actionLoading, setActionLoading] = useState(false);
  const [editMode, setEditMode] = useState(false);
  const [editTitle, setEditTitle, clearTitleDraft] = useDraft(`task-edit:${id}:title`);
  const [editDesc, setEditDesc, clearDescDraft] = useDraft(`task-edit:${id}:desc`);
  const [editProject, setEditProject, clearProjectDraft] = useDraft(`task-edit:${id}:project`);
  const [editNotifyAt, setEditNotifyAt, clearNotifyDraft] = useDraft(`task-edit:${id}:notifyAt`);
  const [showRemindPicker, setShowRemindPicker] = useState(false);

  const clearAllDrafts = () => {
    clearTitleDraft(); clearDescDraft(); clearProjectDraft(); clearNotifyDraft();
  };

  const pollRef = useRef(null);
  const visible = usePageVisible();
  const { sendWsMessage } = useWebSocket();

  const load = useCallback(async () => {
    try {
      const data = await fetchTaskV2(id);
      setTask(data);
      taskDetailCache.set(id, { task: data });
      setError(null);
    } catch (err) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  }, [id]);

  const TERMINAL_STATUSES = new Set(["COMPLETE", "CANCELLED", "FAILED", "TIMEOUT", "REJECTED"]);

  // Register viewing for notification suppression
  useEffect(() => { registerViewingTasks(); return () => unregisterViewingTasks(); }, []);

  useEffect(() => {
    if (!visible) return;
    load();
    if (task && TERMINAL_STATUSES.has(task.status)) return;
    pollRef.current = setInterval(load, POLL_INTERVAL);
    return () => clearInterval(pollRef.current);
  }, [load, visible, task?.status]);

  const agentId = task?.agent_id;
  useEffect(() => {
    if (!agentId) return;
    sendWsMessage({ type: "viewing", agent_id: agentId });
    return () => sendWsMessage({ type: "viewing", agent_id: null, _unview: agentId });
  }, [agentId, sendWsMessage]);

  const loadRef = useRef(load);
  loadRef.current = load;
  useWsEvent((event) => {
    if (event.type === "task_update") {
      if (event.data?.task_id === id) loadRef.current();
    } else if (event.type === "tasks_invalidated") {
      const ids = event.data?.task_ids || [];
      if (ids.includes(id)) loadRef.current();
    }
  }, [id]);

  const toast = useToast();
  const doAction = async (fn, ...args) => {
    setActionLoading(true);
    try {
      await fn(...args);
      await load();
    } catch (err) {
      toast.error(err.message);
    } finally {
      setActionLoading(false);
    }
  };

  const doDelete = async () => {
    setActionLoading(true);
    try {
      await cancelTask(id);
      taskDetailCache.invalidate(id);
      navigate("/tasks", { replace: true });
    } catch (err) {
      toast.error(err.message);
      setActionLoading(false);
    }
  };

  const handleSaveEdit = async () => {
    const updates = {};
    if (editTitle && editTitle !== task.title) updates.title = editTitle;
    if (editDesc !== (task.description || "")) updates.description = editDesc;
    if (editProject && editProject !== task.project_name) updates.project_name = editProject;
    const origNotify = toLocalInputValue(task.notify_at);
    if (editNotifyAt !== origNotify) updates.notify_at = editNotifyAt ? new Date(editNotifyAt).toISOString() : null;
    if (Object.keys(updates).length > 0) {
      await doAction(updateTaskV2, id, updates);
    }
    clearAllDrafts();
    setEditMode(false);
  };

  // Auto-enter edit mode if drafts exist (crash recovery)
  const draftCheckedRef = useRef(false);
  useEffect(() => {
    if (!task || draftCheckedRef.current) return;
    draftCheckedRef.current = true;
    const canEditTask = task.status === "INBOX" || task.status === "PLANNING";
    if (canEditTask) {
      const hasDraft = localStorage.getItem(`draft:task-edit:${id}:title`) !== null
        || localStorage.getItem(`draft:task-edit:${id}:desc`) !== null;
      if (hasDraft) setEditMode(true);
    }
  }, [task, id]);

  const startEdit = () => {
    setEditTitle(task.title);
    setEditDesc(task.description || "");
    setEditProject(task.project_name || "");
    setEditNotifyAt(toLocalInputValue(task.notify_at));
    setEditMode(true);
  };

  // The seed (brief or full cache) means we usually have `task` set even
  // while loading is true — in that case the existing render path uses
  // the brief data for the header, which paints immediately. Only fall
  // back to the full skeleton when we have nothing.
  if (!task) {
    if (loading) return <TaskDetailSkeleton />;
    // No task and not loading → unreachable in practice; let the error
    // branch below handle it.
  }

  if (error && !task) {
    return (
      <div className="h-full flex flex-col items-center justify-center gap-3">
        <p className="text-red-400 text-sm">{error}</p>
        <button type="button" onClick={() => navigate("/tasks")} className="text-xs text-label underline">
          Back to tasks
        </button>
      </div>
    );
  }

  if (!task) return null;

  const dotColor = TASK_STATUS_COLORS[task.status] || "bg-gray-500";
  const textColor = TASK_STATUS_TEXT_COLORS[task.status] || "text-dim";
  const projColor = task.project_name ? projectBadgeColor(task.project_name) : "";
  const canEdit = task.status === "INBOX" || task.status === "PLANNING";

  // Elapsed time for running tasks
  const elapsed = task.started_at && !task.completed_at
    ? Math.floor((serverNow() - new Date(task.started_at).getTime()) / 1000)
    : task.elapsed_seconds;
  const fmtElapsed = (s) => {
    if (!s) return null;
    if (s < 60) return `${s}s`;
    if (s < 3600) return `${Math.floor(s / 60)}m ${s % 60}s`;
    return `${Math.floor(s / 3600)}h ${Math.floor((s % 3600) / 60)}m`;
  };

  return (
    <div className="h-full flex flex-col">
      {/* Header */}
      <div className="shrink-0 bg-page border-b border-divider px-4 pt-4 pb-3 z-10 safe-area-pt">
        <div className="flex items-center gap-2 mb-1.5">
          <button type="button" onClick={() => navigate("/tasks")} className="flex items-center gap-1 text-sm text-label hover:text-heading">
            <svg className="w-4 h-4" fill="none" stroke="currentColor" strokeWidth={2} viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" d="M15 19l-7-7 7-7" />
            </svg>
            Tasks
          </button>
          <div className="ml-auto flex items-center gap-1.5">
            <span className={`w-2 h-2 rounded-full ${dotColor}`} />
            <span className={`text-xs font-medium ${textColor}`}>{task.status}</span>
          </div>
        </div>
        <h1 className="text-lg font-bold text-heading leading-tight">{task.title}</h1>

        {/* Metadata row */}
        <div className="flex flex-wrap items-center gap-1.5 mt-1.5">
          {task.project_name && (
            <span className={`text-[10px] font-medium rounded-full px-2 py-0.5 ${projColor}`}>
              {task.project_name}
            </span>
          )}
          {task.model && (
            <span className="text-[10px] px-1.5 py-0.5 rounded bg-elevated text-label">
              {modelDisplayName(task.model)}
            </span>
          )}
          {task.effort && (
            <span className="text-[10px] px-1.5 py-0.5 rounded bg-elevated text-label uppercase">
              {task.effort}
            </span>
          )}
          {task.use_worktree && (
            <span className="text-[10px] px-1.5 py-0.5 rounded bg-purple-500/15 text-purple-600 dark:text-purple-400">Worktree</span>
          )}
          {task.sync_mode && (
            <span className="text-[10px] px-1.5 py-0.5 rounded bg-emerald-500/15 text-emerald-400">Tmux</span>
          )}
          {task.attempt_number > 1 && (
            <span className="text-[10px] px-1.5 py-0.5 rounded bg-orange-500/15 text-orange-400">Attempt #{task.attempt_number}</span>
          )}
          <span className="text-[10px] text-faint">{relativeTime(task.created_at)}</span>
          {elapsed != null && (
            <span className="text-[10px] text-dim">{fmtElapsed(elapsed)}</span>
          )}
          {task.notify_at && (
            <span className="text-[10px] text-amber-400 flex items-center gap-0.5">
              <Bell className="w-3 h-3" strokeWidth={2} />
              {relativeTime(task.notify_at)}
            </span>
          )}
        </div>
      </div>

      {/* Body */}
      <div className="flex-1 overflow-y-auto overflow-x-hidden">
        <div className="max-w-2xl mx-auto w-full pb-24 p-4 space-y-3">
          {error && !task && (
            <div className="bg-red-950/40 border border-red-800 rounded-xl p-3">
              <p className="text-red-400 text-sm">{error}</p>
            </div>
          )}

          {/* INBOX/PLANNING: editable fields */}
          {canEdit && editMode ? (
            <div className="rounded-xl bg-surface shadow-card p-4 space-y-3">
              <div>
                <label className="block text-xs font-medium text-label mb-1">Title</label>
                <input
                  autoComplete="off"
                  type="text"
                  value={editTitle}
                  onChange={(e) => setEditTitle(e.target.value)}
                  className="w-full rounded-lg bg-input border border-edge px-3 py-2 text-heading text-sm focus:border-accent focus:outline-none"
                />
              </div>
              <div>
                <label className="block text-xs font-medium text-label mb-1">Description</label>
                <textarea
                  value={editDesc}
                  onChange={(e) => setEditDesc(e.target.value)}
                  rows={4}
                  className="w-full rounded-lg bg-input border border-edge px-3 py-2 text-heading text-sm resize-none focus:border-accent focus:outline-none"
                />
              </div>
              <div>
                <label className="block text-xs font-medium text-label mb-1">Project</label>
                <ProjectSelector value={editProject} onChange={setEditProject} />
              </div>
              <div>
                <label className="block text-xs font-medium text-label mb-1">Remind At</label>
                <div className="relative">
                  <button type="button" onClick={() => setShowRemindPicker(v => !v)}
                    className={`w-full flex items-center gap-2 rounded-lg border px-3 py-2 text-sm transition-colors ${
                      editNotifyAt ? "bg-amber-500/10 border-amber-500/30 text-amber-400" : "bg-input border-edge text-dim hover:text-heading"
                    }`}>
                    <Bell className="w-4 h-4 shrink-0" strokeWidth={2} />
                    {editNotifyAt ? new Date(editNotifyAt).toLocaleString([], DATE_SHORT) : "Set reminder"}
                  </button>
                  {showRemindPicker && (
                    <SendLaterPicker
                      onSelect={(iso) => { setEditNotifyAt(toLocalInputValue(iso)); setShowRemindPicker(false); }}
                      onClose={() => setShowRemindPicker(false)}
                      onClear={editNotifyAt ? () => { setEditNotifyAt(""); setShowRemindPicker(false); } : undefined}
                    />
                  )}
                </div>
              </div>
              <div className="flex gap-2">
                <button
                  type="button"
                  onClick={handleSaveEdit}
                  disabled={actionLoading}
                  className="px-4 py-2 rounded-lg bg-accent text-accent-ink text-sm font-medium hover:opacity-90 transition-colors"
                >
                  Save
                </button>
                <button
                  type="button"
                  onClick={() => { clearAllDrafts(); setEditMode(false); }}
                  className="px-4 py-2 rounded-lg bg-elevated text-label text-sm hover:text-heading transition-colors"
                >
                  Cancel
                </button>
              </div>
            </div>
          ) : (
            <>
              {/* Description */}
              {task.description && (
                <div className="rounded-xl bg-surface shadow-card p-3">
                  <div className="text-sm text-body prose-sm">{renderMarkdown(task.description, task.project_name)}</div>
                </div>
              )}

              {/* Edit button for editable tasks */}
              {canEdit && (
                <button
                  type="button"
                  onClick={startEdit}
                  className="w-full rounded-xl bg-surface shadow-card p-3 flex items-center gap-2 text-sm text-label hover:text-heading transition-colors"
                >
                  <svg className="w-4 h-4 text-accent" fill="none" stroke="currentColor" strokeWidth={2} viewBox="0 0 24 24">
                    <path strokeLinecap="round" strokeLinejoin="round" d="M11 5H6a2 2 0 00-2 2v11a2 2 0 002 2h11a2 2 0 002-2v-5m-1.414-9.414a2 2 0 112.828 2.828L11.828 15H9v-2.828l8.586-8.586z" />
                  </svg>
                  Edit task details
                </button>
              )}

              {/* No-project warning for INBOX */}
              {task.status === "INBOX" && !task.project_name && (
                <div className="rounded-xl bg-amber-500/10 border border-amber-500/20 px-3 py-2.5 flex items-start gap-2">
                  <svg className="w-4 h-4 text-amber-400 mt-0.5 shrink-0" fill="none" stroke="currentColor" strokeWidth={2} viewBox="0 0 24 24">
                    <path strokeLinecap="round" strokeLinejoin="round" d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-2.999L13.732 4.001c-.77-1.333-2.694-1.333-3.464 0L3.34 16.001C2.57 17.334 3.532 19 5.072 19z" />
                  </svg>
                  <p className="text-xs text-amber-400/80">Assign a project to enable planning and dispatch</p>
                </div>
              )}
            </>
          )}

          {/* ── Timeline card ── */}
          {(() => {
            const fmtTs = (iso) => {
              if (!iso) return null;
              let s = String(iso);
              if (/^\d{4}-\d{2}-\d{2}T[\d:.]+$/.test(s)) s += "Z";
              return new Date(s).toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
            };
            const steps = [
              { label: "Created", ts: task.created_at, icon: "plus", color: "text-accent", bg: "bg-accent" },
            ];
            if (task.started_at) {
              steps.push({ label: "Started", ts: task.started_at, icon: "play", color: "text-accent", bg: "bg-accent",
                dur: durationDisplay(task.created_at, task.started_at) });
            }
            if (task.completed_at) {
              const termLabel = task.status === "COMPLETE" ? "Completed" : task.status === "FAILED" ? "Failed" : task.status === "TIMEOUT" ? "Timed Out" : task.status === "CANCELLED" ? "Dropped" : task.status === "REJECTED" ? "Rejected" : "Ended";
              const termColor = task.status === "COMPLETE" ? "text-ok" : task.status === "CANCELLED" ? "text-gray-400" : "text-danger";
              const termBg = task.status === "COMPLETE" ? "bg-ok" : task.status === "CANCELLED" ? "bg-gray-500" : "bg-danger";
              steps.push({ label: termLabel, ts: task.completed_at, icon: "end", color: termColor, bg: termBg,
                dur: task.started_at ? durationDisplay(task.started_at, task.completed_at) : null });
            }
            if (steps.length < 2) return null;
            return (
              <div className="rounded-xl bg-surface shadow-card p-3">
                <div className="text-faint text-[10px] uppercase tracking-wider font-medium mb-2.5">Timeline</div>
                <div className="relative pl-5">
                  {/* Vertical line */}
                  <div className="absolute left-[7px] top-1 bottom-1 w-px bg-edge" />
                  {steps.map((step, i) => (
                    <div key={step.label} className={`relative flex items-start gap-3 ${i < steps.length - 1 ? "pb-4" : ""}`}>
                      {/* Dot */}
                      <div className={`absolute -left-5 top-0.5 w-[15px] h-[15px] rounded-full border-2 border-page flex items-center justify-center ${step.bg}`}>
                        {step.icon === "plus" && (
                          <svg className="w-2 h-2 text-white" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={4}>
                            <path strokeLinecap="round" d="M12 5v14M5 12h14" />
                          </svg>
                        )}
                        {step.icon === "play" && (
                          <svg className="w-2 h-2 text-white" viewBox="0 0 24 24" fill="currentColor">
                            <path d="M8 5v14l11-7z" />
                          </svg>
                        )}
                        {step.icon === "end" && (
                          <svg className="w-2 h-2 text-white" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={4}>
                            <path strokeLinecap="round" strokeLinejoin="round" d="M4.5 12.75l6 6 9-13.5" />
                          </svg>
                        )}
                      </div>
                      {/* Content */}
                      <div className="flex-1 min-w-0">
                        <div className="flex items-center gap-2">
                          <span className={`text-xs font-medium ${step.color}`}>{step.label}</span>
                          {step.dur && <span className="text-[10px] text-faint">{step.dur}</span>}
                        </div>
                        <div className="flex items-center gap-2 mt-0.5">
                          <span className="text-[11px] text-dim">{fmtTs(step.ts)}</span>
                          <span className="text-[10px] text-faint">{relativeTime(step.ts)}</span>
                        </div>
                      </div>
                    </div>
                  ))}
                </div>
              </div>
            );
          })()}

          {/* ── Agent card — shown for any status with agent_id ── */}
          {task.agent_id && (
            <div className="rounded-xl bg-surface shadow-card p-3">
              <div className="flex items-center gap-2">
                {task.status === "EXECUTING" ? (
                  <span className="w-2 h-2 rounded-full bg-accent animate-pulse" />
                ) : (
                  <svg className="w-3.5 h-3.5 text-dim shrink-0" fill="none" stroke="currentColor" strokeWidth={2} viewBox="0 0 24 24">
                    <path strokeLinecap="round" strokeLinejoin="round" d="M9.75 3.104v5.714a2.25 2.25 0 01-.659 1.591L5 14.5M9.75 3.104c-.251.023-.501.05-.75.082m.75-.082a24.301 24.301 0 014.5 0m0 0v5.714a2.25 2.25 0 00.659 1.591L19 14.5M14.25 3.104c.251.023.501.05.75.082M19 14.5l-2.47-2.47" />
                  </svg>
                )}
                <span className="text-xs text-label font-medium">Agent</span>
                <code className="text-[10px] text-faint font-mono">{task.agent_id.slice(0, 8)}</code>
                <button
                  type="button"
                  onClick={() => navigate(`/agents/${task.agent_id}`, { state: forwardState(location) })}
                  className="ml-auto inline-flex items-center gap-1.5 px-2.5 py-1 rounded-lg accent-tint-10 text-xs text-accent hover:accent-tint-25 transition-colors font-medium"
                >
                  <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" strokeWidth={2} viewBox="0 0 24 24">
                    <path strokeLinecap="round" strokeLinejoin="round" d="M8 12h.01M12 12h.01M16 12h.01M21 12c0 4.418-4.03 8-9 8a9.863 9.863 0 01-4.255-.949L3 20l1.395-3.72C3.512 15.042 3 13.574 3 12c0-4.418 4.03-8 9-8s9 3.582 9 8z" />
                  </svg>
                  View conversation
                </button>
              </div>
            </div>
          )}

          {/* ── Last agent message preview ── */}
          {task.last_agent_message && (
            <div className="rounded-xl bg-surface shadow-card p-3">
              <div className="text-faint text-[10px] uppercase tracking-wider font-medium mb-1.5">Last Agent Message</div>
              <div className="text-sm text-body bg-inset rounded-lg p-2.5 max-h-[120px] overflow-y-auto">
                <p className="line-clamp-4 whitespace-pre-wrap">{task.last_agent_message}</p>
              </div>
            </div>
          )}

          {/* ── Status-specific cards ── */}

          {/* REVIEW: agent summary + verification */}
          {task.status === "REVIEW" && (
            <div className="rounded-xl bg-surface shadow-card p-3 space-y-3">
              <h3 className="text-sm font-medium text-amber-400">Ready for Review</h3>
              {task.agent_summary ? (
                <div className="text-sm text-body bg-inset rounded-lg p-3 max-h-[400px] overflow-y-auto">
                  {renderMarkdown(task.agent_summary, task.project_name)}
                </div>
              ) : (
                <p className="text-sm text-dim">Agent completed — review the conversation below.</p>
              )}
              {(() => {
                const arts = task.review_artifacts ? (() => { try { return JSON.parse(task.review_artifacts); } catch { return {}; } })() : {};
                if (!arts.verify_status || arts.verify_status === "running") return null;
                const colors = { pass: "text-ok ok-tint-10", fail: "text-danger danger-tint-10", warn: "text-amber-600 dark:text-amber-400 bg-amber-500/10", done: "text-accent accent-tint-10", error: "text-danger danger-tint-10" };
                const labels = { pass: "Verification Passed", fail: "Verification Failed", warn: "Verification Warning", done: "Verification Complete", error: "Verification Error" };
                return (
                  <div className="space-y-2">
                    <h4 className={`text-xs font-medium ${(colors[arts.verify_status] || "text-dim").split(" ")[0]}`}>{labels[arts.verify_status] || "Verification"}</h4>
                    {arts.verify_result && (
                      <div className={`text-xs rounded-lg p-3 max-h-[300px] overflow-y-auto ${colors[arts.verify_status] || "bg-inset text-body"}`}>
                        <pre className="whitespace-pre-wrap font-mono">{arts.verify_result}</pre>
                      </div>
                    )}
                    {arts.verify_agent_id && (
                      <button
                        type="button"
                        onClick={() => navigate(`/agents/${arts.verify_agent_id}`, { state: forwardState(location) })}
                        className="inline-flex items-center gap-1 text-xs text-accent hover:opacity-80"
                      >
                        View verification agent
                      </button>
                    )}
                  </div>
                );
              })()}
            </div>
          )}

          {/* MERGING */}
          {task.status === "MERGING" && (
            <div className="rounded-xl bg-surface shadow-card p-3 flex items-center gap-3">
              <span className="w-2 h-2 rounded-full bg-purple-500 animate-pulse" />
              <span className="text-sm text-label">Merging branch...</span>
              {task.merge_agent_id && (
                <a
                  href={`/agents/${task.merge_agent_id}`}
                  className="text-xs text-accent hover:underline ml-auto"
                  onClick={(e) => { e.preventDefault(); navigate(`/agents/${task.merge_agent_id}`, { state: forwardState(location) }); }}
                >
                  View merge agent
                </a>
              )}
            </div>
          )}

          {/* CONFLICT */}
          {task.status === "CONFLICT" && (
            <div className="rounded-xl bg-surface shadow-card p-3 space-y-2">
              <h3 className="text-sm font-medium text-red-400">Merge Conflict</h3>
              {task.error_message && (
                <pre className="text-xs text-body bg-inset rounded-lg p-3 overflow-x-auto">{task.error_message}</pre>
              )}
            </div>
          )}

          {/* COMPLETE: agent summary */}
          {task.status === "COMPLETE" && task.agent_summary && (
            <div className="rounded-xl bg-surface shadow-card p-3 space-y-2">
              <div className="text-faint text-[10px] uppercase tracking-wider font-medium">Agent Summary</div>
              <div className="text-sm text-body">{renderMarkdown(task.agent_summary, task.project_name)}</div>
            </div>
          )}

          {/* REJECTED */}
          {task.status === "REJECTED" && (
            <div className="rounded-xl bg-surface shadow-card p-3 space-y-2">
              <h3 className="text-sm font-medium text-orange-400">Rejected</h3>
              {task.rejection_reason && (
                <p className="text-sm text-body">{task.rejection_reason}</p>
              )}
            </div>
          )}

          {/* FAILED / TIMEOUT: error message */}
          {(task.status === "FAILED" || task.status === "TIMEOUT") && (
            <div className="rounded-xl bg-surface shadow-card p-3 space-y-2">
              <h3 className="text-sm font-medium text-red-400">{task.status === "TIMEOUT" ? "Timed Out" : "Failed"}</h3>
              {task.error_message && (
                <pre className="text-xs text-body bg-inset rounded-lg p-3 overflow-x-auto whitespace-pre-wrap">{task.error_message}</pre>
              )}
            </div>
          )}

          {/* Retry history */}
          {task.attempt_number > 1 && task.retry_context && (
            <details className="rounded-xl bg-surface shadow-card p-3">
              <summary className="text-sm font-medium text-label cursor-pointer">
                Previous attempt context
              </summary>
              <pre className="mt-2 text-xs text-body bg-inset rounded-lg p-3 overflow-x-auto whitespace-pre-wrap">
                {task.retry_context}
              </pre>
            </details>
          )}

          {/* ── Metadata details ── */}
          <details className="rounded-xl bg-surface shadow-card p-3">
            <summary className="text-xs font-medium text-label cursor-pointer flex items-center gap-2">
              <svg className="w-3.5 h-3.5 text-dim" fill="none" stroke="currentColor" strokeWidth={2} viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" d="M11.25 11.25l.041-.02a.75.75 0 011.063.852l-.708 2.836a.75.75 0 001.063.853l.041-.021M21 12a9 9 0 11-18 0 9 9 0 0118 0zm-9-3.75h.008v.008H12V8.25z" />
              </svg>
              Details
            </summary>
            <div className="mt-2.5 space-y-2">
              {/* Task ID */}
              <div className="flex items-center justify-between">
                <span className="text-xs text-label">Task ID</span>
                <code className="text-[11px] text-dim font-mono select-all">{task.id}</code>
              </div>
              {/* Agent ID */}
              {task.agent_id && (
                <div className="flex items-center justify-between">
                  <span className="text-xs text-label">Agent ID</span>
                  <code className="text-[11px] text-dim font-mono select-all">{task.agent_id}</code>
                </div>
              )}
              {/* Project */}
              {task.project_name && (
                <div className="flex items-center justify-between">
                  <span className="text-xs text-label">Project</span>
                  <span className="text-[11px] text-dim">{task.project_name}</span>
                </div>
              )}
              {/* Model */}
              {task.model && (
                <div className="flex items-center justify-between">
                  <span className="text-xs text-label">Model</span>
                  <span className="text-[11px] text-dim">{modelDisplayName(task.model)}</span>
                </div>
              )}
              {/* Effort */}
              {task.effort && (
                <div className="flex items-center justify-between">
                  <span className="text-xs text-label">Effort</span>
                  <span className="text-[11px] text-dim capitalize">{task.effort}</span>
                </div>
              )}
              {/* Branch */}
              {task.branch_name && (
                <div className="flex items-center justify-between">
                  <span className="text-xs text-label">Branch</span>
                  <code className="text-[11px] text-dim font-mono truncate max-w-[180px] select-all">{task.branch_name}</code>
                </div>
              )}
              {/* Worktree */}
              {task.worktree_name && (
                <div className="flex items-center justify-between">
                  <span className="text-xs text-label">Worktree</span>
                  <span className="text-[11px] text-dim truncate max-w-[180px]">{task.worktree_name}</span>
                </div>
              )}
              {/* Attempt */}
              {task.attempt_number > 1 && (
                <div className="flex items-center justify-between">
                  <span className="text-xs text-label">Attempt</span>
                  <span className="text-[11px] text-dim">#{task.attempt_number}</span>
                </div>
              )}
              {/* Flags */}
              <div className="flex items-center justify-between">
                <span className="text-xs text-label">Flags</span>
                <div className="flex items-center gap-1.5">
                  {task.use_worktree && <span className="text-[10px] px-1.5 py-px rounded bg-purple-500/15 text-purple-600 dark:text-purple-400">Worktree</span>}
                  {task.sync_mode && <span className="text-[10px] px-1.5 py-px rounded bg-emerald-500/15 text-emerald-600 dark:text-emerald-400">Sync</span>}
                  {task.skip_permissions && <span className="text-[10px] px-1.5 py-px rounded bg-amber-500/15 text-amber-600 dark:text-amber-400">Skip perms</span>}
                  {!task.use_worktree && !task.sync_mode && !task.skip_permissions && <span className="text-[11px] text-faint">None</span>}
                </div>
              </div>
              {/* Elapsed */}
              {elapsed != null && (
                <div className="flex items-center justify-between">
                  <span className="text-xs text-label">Elapsed</span>
                  <span className="text-[11px] text-dim">{fmtElapsed(elapsed)}</span>
                </div>
              )}
            </div>
          </details>
        </div>
      </div>

      {/* Action bar */}
      <div className="shrink-0 border-t border-divider bg-page px-4 py-3 flex flex-wrap gap-2 justify-center">
        {/* INBOX → Dispatch + Delete */}
        {task.status === "INBOX" && (
          <>
            <button
              type="button"
              onClick={() => doAction(dispatchTask, id)}
              disabled={actionLoading || !task.project_name}
              title={!task.project_name ? "Set a project before dispatching" : "Dispatch directly"}
              className="whitespace-nowrap px-4 py-2 rounded-lg bg-accent text-accent-ink text-sm font-medium hover:opacity-90 disabled:opacity-50 transition-colors"
            >
              Dispatch
            </button>
            <button
              type="button"
              onClick={() => { if (confirm("Delete this task?")) doDelete(); }}
              disabled={actionLoading}
              className="whitespace-nowrap px-4 py-2 rounded-lg bg-red-600/80 text-white text-sm font-medium hover:bg-red-500 disabled:opacity-50 transition-colors"
            >
              Delete
            </button>
          </>
        )}

        {/* PLANNING → Dispatch + Back to Inbox + Delete */}
        {task.status === "PLANNING" && (
          <>
            <button
              type="button"
              onClick={() => doAction(dispatchTask, id)}
              disabled={actionLoading || !task.project_name}
              className="whitespace-nowrap px-4 py-2 rounded-lg bg-accent text-accent-ink text-sm font-medium hover:opacity-90 disabled:opacity-50 transition-colors"
            >
              Dispatch
            </button>
            <button
              type="button"
              onClick={() => doAction((taskId) => updateTaskV2(taskId, { status: "INBOX" }), id)}
              disabled={actionLoading}
              className="whitespace-nowrap px-4 py-2 rounded-lg bg-elevated text-label text-sm font-medium hover:text-heading disabled:opacity-50 transition-colors"
            >
              Back to Inbox
            </button>
            <button
              type="button"
              onClick={() => { if (confirm("Delete this task?")) doDelete(); }}
              disabled={actionLoading}
              className="whitespace-nowrap px-4 py-2 rounded-lg bg-red-600/80 text-white text-sm font-medium hover:bg-red-500 disabled:opacity-50 transition-colors"
            >
              Delete
            </button>
          </>
        )}

        {/* PENDING → Cancel */}
        {task.status === "PENDING" && (
          <button
            type="button"
            onClick={() => { if (confirm("Cancel this task?")) doDelete(); }}
            disabled={actionLoading}
            className="whitespace-nowrap px-4 py-2 rounded-lg bg-red-600 text-white text-sm font-medium hover:bg-red-500 disabled:opacity-50 transition-colors"
          >
            Cancel
          </button>
        )}

        {/* EXECUTING → Cancel */}
        {task.status === "EXECUTING" && (
          <button
            type="button"
            onClick={() => { if (confirm("Cancel this task?")) doDelete(); }}
            disabled={actionLoading}
            className="whitespace-nowrap px-4 py-2 rounded-lg bg-red-600 text-white text-sm font-medium hover:bg-red-500 disabled:opacity-50 transition-colors"
          >
            Cancel
          </button>
        )}

        {/* REVIEW / CONFLICT → Delete only */}
        {(task.status === "REVIEW" || task.status === "CONFLICT") && (
          <button
            type="button"
            onClick={() => { if (confirm("Delete this task?")) doDelete(); }}
            disabled={actionLoading}
            className="whitespace-nowrap px-4 py-2 rounded-lg bg-red-600/80 text-white text-sm font-medium hover:bg-red-500 disabled:opacity-50 transition-colors"
          >
            Delete
          </button>
        )}

        {/* REJECTED / FAILED / TIMEOUT → Retry + Delete */}
        {(task.status === "REJECTED" || task.status === "FAILED" || task.status === "TIMEOUT") && (
          <>
            <button
              type="button"
              onClick={() => doAction(dispatchTask, id)}
              disabled={actionLoading || !task.project_name}
              className="whitespace-nowrap px-4 py-2 rounded-lg bg-accent text-accent-ink text-sm font-medium hover:opacity-90 disabled:opacity-50 transition-colors"
            >
              Retry
            </button>
            <button
              type="button"
              onClick={() => { if (confirm("Delete this task?")) doDelete(); }}
              disabled={actionLoading}
              className="whitespace-nowrap px-4 py-2 rounded-lg bg-red-600/80 text-white text-sm font-medium hover:bg-red-500 disabled:opacity-50 transition-colors"
            >
              Delete
            </button>
          </>
        )}
      </div>
    </div>
  );
}
