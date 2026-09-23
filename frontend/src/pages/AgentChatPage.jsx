import React, { useState, useEffect, useLayoutEffect, useCallback, useRef, useMemo, Component, lazy, Suspense } from "react";
import { createPortal } from "react-dom";
import { Bell, BellOff, Hourglass, SquareTerminal } from "lucide-react";
import { useParams, useNavigate, useLocation } from "react-router-dom";
import {
  fetchAgent,
  fetchDisplaySent,
  fetchDisplayPreSent,
  sendMessage,
  stopAgent,
  resumeAgent,
  renameAgent,
  markAgentRead,
  fetchProjectSessions,
  starSession,
  unstarSession,
  cancelMessage,
  deleteMessage,
  updateMessage,
  updateAgent,
  answerAgent,
  escapeAgent,
  uploadFile,
  fetchProjectFile,
  respondPermission,
  fetchAgentSuggestions,
  fetchProcessedSuggestions,
  applyAgentSuggestions,
  discardAgentSuggestions,
  regenerateAgentInsights,
  fetchTaskV2,
  wakeSync,
  createBookmark,
  deleteBookmark,
  fetchProjectBookmarks,
  clog,
} from "../lib/api";
import ProjectFileModal from "../components/ProjectFileModal";
import FloatingTaskCard from "../components/FloatingTaskCard";
import ProjectBrowserModal from "../components/ProjectBrowserModal";
// Lazy: pulls in xterm.js (~300KB) only when the terminal is opened
const TerminalOverlay = lazy(() => import("../components/TerminalOverlay"));
import { relativeTime, renderMarkdown, extractFileAttachments, stripAttachmentTags } from "../lib/formatters";
import { serverNow } from "../lib/serverTime";
import { uploadUrl } from "../lib/urls";
import { forwardState, resolveBack } from "../lib/nav";
import { getEinkMode } from "../lib/einkMode";

// Mini error boundary that wraps individual markdown renders so a single
// broken message doesn't crash the entire chat page.
class SafeMarkdown extends Component {
  constructor(props) {
    super(props);
    this.state = { hasError: false };
  }
  static getDerivedStateFromError() {
    return { hasError: true };
  }
  componentDidCatch(error, info) {
    console.error("SafeMarkdown caught:", error, info);
  }
  render() {
    if (this.state.hasError) {
      return <pre className="text-sm text-body whitespace-pre-wrap">{this.props.fallback || "Error rendering message"}</pre>;
    }
    return this.props.children;
  }
}
import FileAttachments from "../components/FilePreview";
import { WebAppCardBubble } from "../components/WebAppPreview";
import BookmarkNotePrompt from "../components/BookmarkNotePrompt";
import DivergePrompt from "../components/DivergePrompt";
import { safeSetItem } from "../lib/safeStorage";
import ImageLightbox from "../components/ImageLightbox";
import {
  AGENT_STATUS_COLORS, AGENT_STATUS_TEXT_COLORS, modelDisplayName,
  POLL_ACTIVE_INTERVAL, POLL_IDLE_INTERVAL,
  COPY_TOAST_DURATION, ERROR_TOAST_DURATION, TOAST_DURATION,
  ESCAPE_COOLDOWN, LONG_PRESS_DELAY, DOUBLE_TAP_WINDOW,
  SCROLL_SAVE_DEBOUNCE, JSONL_FLUSH_DELAY_MS, SYNC_SETTLE_DELAY,
  isSystemHealthy,
} from "../lib/constants";
import { DATE_SHORT, TIME_SHORT } from "../lib/formatters";
import VoiceRecorder from "../components/VoiceRecorder";
import { appendVoiceDraft, deleteVoiceJob } from "../lib/voiceStore";
import WorktreePill from "../components/WorktreePill";
import PopoverArrow from "../components/PopoverArrow";
import ContextUsagePill from "../components/ContextUsagePill";
import useDraft from "../hooks/useDraft";
import useVoiceRecorder from "../hooks/useVoiceRecorder";
import useWebSocket, { useWsEvent, isAgentMuted, setAgentMuted, clearAgentNotified, registerViewing, unregisterViewing, touchAgentInteraction } from "../hooks/useWebSocket";
import useHealthStatus from "../hooks/useHealthStatus";
import useContextUsage from "../hooks/useContextUsage";
import { useMonitor } from "../contexts/MonitorContext";
import usePageVisible from "../hooks/usePageVisible";
import { useToast } from "../contexts/ToastContext";
import ChatSkeleton from "../components/skeletons/ChatSkeleton";
import { agentBriefCache, processedSuggestionsCache, pendingSuggestionsCache } from "../lib/detailCache";
import { consumePrefetch } from "../lib/chatPrefetch";

const ACTIVE_AGENT_STATUSES = new Set(["EXECUTING", "IDLE"]);

// Walk text nodes under `root` and wrap case-insensitive occurrences of
// `query` in <mark class="search-hit"> elements. Used by the search-result
// focus path to give the matched text a yellow find-bar highlight in
// addition to the bubble's breathing flash. Skips already-marked nodes
// to keep re-runs idempotent. Browsers apply the mutation synchronously,
// so calling this inside the focus useLayoutEffect lands the highlight
// in the same paint as the scroll.
function highlightTextMatches(root, query) {
  if (!root || !query) return;
  const needle = query.toLowerCase();
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
    acceptNode(node) {
      if (!node.nodeValue || !node.nodeValue.toLowerCase().includes(needle)) {
        return NodeFilter.FILTER_REJECT;
      }
      // Skip text already inside a search-hit (idempotent re-runs)
      if (node.parentElement?.closest(".search-hit")) {
        return NodeFilter.FILTER_REJECT;
      }
      return NodeFilter.FILTER_ACCEPT;
    },
  });
  const targets = [];
  let n;
  while ((n = walker.nextNode())) targets.push(n);
  for (const node of targets) {
    const text = node.nodeValue;
    const lower = text.toLowerCase();
    const frag = document.createDocumentFragment();
    let i = 0;
    while (i < text.length) {
      const hit = lower.indexOf(needle, i);
      if (hit < 0) {
        frag.appendChild(document.createTextNode(text.slice(i)));
        break;
      }
      if (hit > i) frag.appendChild(document.createTextNode(text.slice(i, hit)));
      const mark = document.createElement("mark");
      mark.className = "search-hit";
      mark.textContent = text.slice(hit, hit + needle.length);
      frag.appendChild(mark);
      i = hit + needle.length;
    }
    node.parentNode.replaceChild(frag, node);
  }
}

// --- Chat Bubble ---

function SystemBubble({ message }) {
  const [expanded, setExpanded] = useState(false);
  const isLong = message.content.length > 80;
  const label = isLong
    ? message.content.slice(0, 60).replace(/\n/g, " ") + "..."
    : message.content;

  return (
    <div className="flex justify-center my-2">
      <button
        type="button"
        onClick={() => isLong && setExpanded((v) => !v)}
        className={`inline-block max-w-[80%] px-3 py-1 rounded-lg bg-elevated text-xs text-dim text-left ${isLong ? "cursor-pointer hover:bg-hover transition-colors" : "cursor-default"}`}
      >
        <div className="flex items-center gap-1.5">
          <span className="shrink-0 opacity-60">sys</span>
          <span className="truncate">{label}</span>
          <span className="shrink-0 opacity-40 ml-1">{relativeTime(message.completed_at || message.created_at)}</span>
          {isLong && (
            <svg className={`w-3 h-3 shrink-0 opacity-50 transition-transform ${expanded ? "rotate-180" : ""}`} fill="none" stroke="currentColor" strokeWidth={2} viewBox="0 0 24 24">
              <path strokeLinecap="round" d="m19 9-7 7-7-7" />
            </svg>
          )}
        </div>
        {expanded && (
          <div className="mt-2 pt-2 border-t border-divider text-xs text-dim whitespace-pre-wrap break-words max-h-60 overflow-y-auto">
            {message.content}
          </div>
        )}
      </button>
    </div>
  );
}

// --- Stop note card (Redo / Drop) ---
function StopNoteCard({ message }) {
  const meta = message.metadata || {};
  const isDrop = meta.stop_action ? meta.stop_action === "dropped" : message.content.startsWith("Task dropped");
  const label = isDrop ? "Dropped" : "Redo";
  const note = meta.stop_action ? message.content : message.content.replace(/^(Task dropped|Redo)\s*—\s*/, "");
  const color = isDrop ? "gray" : "amber";
  return (
    <div className={`mx-4 my-3 rounded-xl border px-4 py-3 ${
      isDrop
        ? "bg-gray-500/8 border-gray-500/20"
        : "bg-amber-500/8 border-amber-500/20"
    }`}>
      <div className={`text-[11px] font-semibold mb-1 ${
        isDrop ? "text-gray-400" : "text-amber-500"
      }`}>{label}</div>
      {note && <div className="text-sm text-body whitespace-pre-wrap">{note}</div>}
      <div className="text-[10px] text-dim mt-1">{relativeTime(message.completed_at || message.created_at)}</div>
    </div>
  );
}

// --- Sub-agent task notification bubble (collapsible) ---

function SubAgentBubble({ message, project }) {
  const [expanded, setExpanded] = useState(false);
  const tn = message.metadata?.task_notification;
  const content = message.content || "";
  const status = tn?.status || content.match(/<status>(.*?)<\/status>/s)?.[1] || "";
  const summary = tn?.summary || content.match(/<summary>([\s\S]*?)<\/summary>/)?.[1] || "";
  const result = tn?.result ?? content.match(/<result>([\s\S]*?)<\/result>/)?.[1]?.trim();

  const statusColor = status === "completed"
    ? "text-emerald-400"
    : status === "failed" ? "text-red-400" : "text-amber-400";
  const borderColor = status === "completed"
    ? "border-emerald-500/30"
    : status === "failed" ? "border-red-500/30" : "border-amber-500/30";

  return (
    <div className="flex justify-start my-2">
      <div className="max-w-[min(85%,30rem)]">
        <button
          type="button"
          onClick={() => setExpanded((v) => !v)}
          className={`w-full text-left rounded-2xl px-4 py-2.5 border ${borderColor} bg-purple-900/20 cursor-pointer hover:bg-purple-900/30 transition-colors rounded-bl-md`}
        >
          <div className="flex items-center gap-2">
            <span className="text-xs font-semibold text-purple-400">Sub-agent</span>
            <span className={`text-xs font-medium ${statusColor}`}>{status}</span>
            <svg className={`w-3 h-3 shrink-0 text-dim transition-transform ml-auto ${expanded ? "rotate-180" : ""}`} fill="none" stroke="currentColor" strokeWidth={2} viewBox="0 0 24 24">
              <path strokeLinecap="round" d="m19 9-7 7-7-7" />
            </svg>
          </div>
          {summary && <p className="text-sm text-body mt-1">{summary}</p>}
        </button>
        {expanded && result && (
          <div className={`mt-0 border ${borderColor} border-t-0 rounded-b-2xl bg-purple-900/10 px-4 py-3 max-h-[400px] overflow-y-auto`}>
            <div className="text-sm">
              <SafeMarkdown fallback={result}>
                {renderMarkdown(result, project)}
              </SafeMarkdown>
            </div>
          </div>
        )}
        <div className="text-xs text-dim mt-1 px-1">
          {relativeTime(message.completed_at || message.created_at)}
          {message.source && (
            <span className={`ml-1.5 px-1 py-0.5 rounded text-[10px] font-medium leading-none ${
              message.source === "web"
                ? "accent-tint-20 text-accent"
                : "bg-emerald-500/20 text-emerald-700 dark:text-emerald-300"
            }`}>
              {message.source}
            </span>
          )}
        </div>
      </div>
    </div>
  );
}

// --- RAG Insights bubble (collapsible, shown on user messages) ---

function InsightsBubble({ insights }) {
  const [expanded, setExpanded] = useState(false);
  if (!insights || insights.length === 0) return null;
  return (
    <div className="rounded-lg bg-elevated overflow-hidden max-w-[280px] mt-1.5 ml-auto">
      <button
        type="button"
        onClick={() => setExpanded((v) => !v)}
        className="w-full flex items-center gap-2 px-3 py-2 hover:bg-hover transition-colors text-left"
      >
        <svg className="w-4 h-4 text-accent shrink-0" fill="none" stroke="currentColor" strokeWidth={1.5} viewBox="0 0 24 24">
          <path strokeLinecap="round" strokeLinejoin="round" d="M9.663 17h4.673M12 3v1m6.364 1.636l-.707.707M21 12h-1M4 12H3m3.343-5.657l-.707-.707m2.828 9.9a5 5 0 117.072 0l-.548.547A3.374 3.374 0 0014 18.469V19a2 2 0 11-4 0v-.531c0-.895-.356-1.754-.988-2.386l-.548-.547z" />
        </svg>
        <span className="text-xs text-label flex-1 min-w-0">Past Insights ({insights.length})</span>
        <svg className={`w-3 h-3 text-dim shrink-0 transition-transform ${expanded ? "rotate-180" : ""}`} fill="none" stroke="currentColor" strokeWidth={2} viewBox="0 0 24 24">
          <path strokeLinecap="round" d="m19 9-7 7-7-7" />
        </svg>
      </button>
      {expanded && (
        <div className="border-t border-divider max-h-60 overflow-y-auto">
          {insights.map((insight, i) => {
            const dateMatch = insight.match(/^\[(\d{4}-\d{2}-\d{2})\]\s*/);
            const date = dateMatch ? dateMatch[1] : null;
            const text = dateMatch ? insight.slice(dateMatch[0].length) : insight;
            return (
              <div key={i} className="flex items-start gap-2 px-3 py-1.5 text-left">
                {date && <span className="shrink-0 text-[10px] text-dim font-mono mt-0.5">{date}</span>}
                <span className="text-xs text-label">{text}</span>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}

// --- Interactive: AskUserQuestion ---

function QuestionBubble({ item, agentId, onAnswered }) {
  // Per-question state: { [questionIndex]: optionIndex }
  const [chosenIndices, setChosenIndices] = useState({});
  // Track which question is currently submitting (null = none)
  const [submittingQi, setSubmittingQi] = useState(null);
  const [submitError, setSubmitError] = useState(null);

  const questions = item.questions || [];
  // Detect dismissed/escaped answers (not a real selection)
  const isDismissed = item.answer != null && typeof item.answer === "string" &&
    (item.answer.startsWith("The user doesn't want to proceed") ||
     item.answer.startsWith("User declined") ||
     item.answer.startsWith("Tool use rejected"));

  const handleSubmit = async (qi, idx) => {
    setChosenIndices(prev => ({ ...prev, [qi]: idx }));
    setSubmittingQi(qi);
    setSubmitError(null);
    try {
      await answerAgent(agentId, {
        tool_use_id: item.tool_use_id,
        type: "ask_user_question",
        selected_index: idx,
        question_index: qi,
      });
      onAnswered?.();
    } catch (e) {
      console.error("Failed to answer:", e);
      setSubmitError("Failed to send answer: " + (e.message || "Unknown error"));
      setChosenIndices(prev => { const next = { ...prev }; delete next[qi]; return next; });
    } finally {
      setSubmittingQi(null);
    }
  };

  // Resolve which option index was selected for a specific question
  const resolveIdx = (q, qi) => {
    // Dismissed = no valid selection, regardless of stored indices
    if (isDismissed) return null;
    // 1. Best: per-question index from backend (new field)
    if (item.selected_indices && item.selected_indices[String(qi)] != null)
      return item.selected_indices[String(qi)];
    // 2. Parse the answer string: use positional matching (qi-th ="label"
    //    pair corresponds to qi-th question) to avoid cross-question leakage
    if (item.answer != null && typeof item.answer === "string") {
      const allMatches = [...item.answer.matchAll(/="([^"]+)"/g)];
      // For single-question: try all matches against this question's options
      if (questions.length <= 1) {
        for (const m of allMatches) {
          const idx = (q.options || []).findIndex((o) => o.label === m[1]);
          if (idx !== -1) return idx;
        }
      } else {
        // Multi-question: match labels to questions in order (consumed set)
        const used = new Set();
        for (let qj = 0; qj <= qi; qj++) {
          const opts = (questions[qj]?.options || []);
          for (let mi = 0; mi < allMatches.length; mi++) {
            if (used.has(mi)) continue;
            const matchedOi = opts.findIndex((o) => o.label === allMatches[mi][1]);
            if (matchedOi !== -1) {
              used.add(mi);
              if (qj === qi) return matchedOi;
              break; // move to next question
            }
          }
        }
      }
    }
    // 3. Local optimistic choice for this question
    if (chosenIndices[qi] != null) return chosenIndices[qi];
    // 4. Backward compat: selected_index only applies to first question
    if (qi === 0 && item.selected_index != null) return item.selected_index;
    return null;
  };

  return (
    <div className="mt-3 space-y-3">
      {questions.map((q, qi) => {
        const answeredIdx = resolveIdx(q, qi);
        const isAnswered = answeredIdx != null || isDismissed || item.auto_approved;
        // Sequential lock: question qi is locked if any prior question is unanswered
        const isLocked = qi > 0 && !isDismissed && resolveIdx(questions[qi - 1], qi - 1) == null;

        // Badge text & style
        let badgeText = null;
        let badgeClass = "";
        if (isDismissed) {
          badgeText = "Dismissed";
          badgeClass = "danger-tint-15 text-danger";
        } else if (item.auto_approved) {
          badgeText = "Auto-approved";
          badgeClass = "bg-elevated text-dim";
        } else if (answeredIdx != null) {
          badgeText = "Choice Sent";
          badgeClass = "accent-tint-15 text-accent";
        }

        return (
          <div key={qi} className={`rounded-xl bg-surface p-3 ${isLocked ? "opacity-50" : ""}`}>
            <div className="flex items-center gap-2 mb-1.5">
              {q.header && (
                <span className="inline-block px-2 py-0.5 rounded-full accent-tint-15 text-accent text-[10px] font-semibold uppercase tracking-wider">
                  {q.header}
                </span>
              )}
              {badgeText && (
                <span className={`ml-auto px-2 py-0.5 rounded-full text-[10px] font-semibold uppercase tracking-wider ${badgeClass}`}>
                  {badgeText}
                </span>
              )}
            </div>
            <p className="text-sm text-heading font-medium mb-2">{q.question}</p>
            {isLocked && (
              <p className="text-xs text-dim mb-2 italic">Answer above first</p>
            )}
            <div className="space-y-1.5 max-h-80 overflow-y-auto">
              {(q.options || []).map((opt, oi) => {
                const isChosen = answeredIdx === oi;
                const dimmed = isAnswered && !isChosen;
                const disabled = isAnswered || isLocked || submittingQi === qi;

                return (
                  <button
                    key={oi}
                    type="button"
                    disabled={disabled}
                    onClick={() => {
                      if (!isAnswered && !isLocked) handleSubmit(qi, oi);
                    }}
                    className={`w-full text-left rounded-lg px-3 py-2 text-sm transition-all border ${
                      isChosen
                        ? "bg-hover border-ring-hover text-heading"
                        : dimmed
                          ? "bg-transparent border-ring-hover text-faint"
                          : isLocked
                            ? "bg-transparent border-ring-hover text-faint cursor-not-allowed"
                            : "bg-transparent border-ring-hover hover:bg-elevated text-body"
                    } ${isAnswered || isLocked ? "cursor-default" : "cursor-pointer"}`}
                  >
                    <div className="flex items-start gap-2">
                      <span className={`mt-0.5 w-4 h-4 rounded-full border-2 shrink-0 flex items-center justify-center ${
                        isChosen ? "border-accent bg-accent" : "border-ring-hover"
                      }`}>
                        {isChosen && (
                          <svg className="w-2.5 h-2.5 text-white" fill="currentColor" viewBox="0 0 20 20">
                            <path fillRule="evenodd" d="M16.707 5.293a1 1 0 010 1.414l-8 8a1 1 0 01-1.414 0l-4-4a1 1 0 011.414-1.414L8 12.586l7.293-7.293a1 1 0 011.414 0z" clipRule="evenodd" />
                          </svg>
                        )}
                      </span>
                      <div>
                        <span className="font-medium">{opt.label}</span>
                        {opt.description && (
                          <p className={`text-xs mt-0.5 ${dimmed ? "text-dim/30" : "text-dim"}`}>{opt.description}</p>
                        )}
                      </div>
                    </div>
                  </button>
                );
              })}
            </div>
            {submittingQi === qi && (
              <p className="text-xs text-dim mt-2 flex items-center gap-1.5">
                <span className="inline-block w-1.5 h-1.5 rounded-full bg-accent animate-pulse" />
                Sending answer...
              </p>
            )}
          </div>
        );
      })}
      {submitError && (
        <p className="text-xs text-red-400 mt-2 px-1">{submitError}</p>
      )}
    </div>
  );
}

// --- Interactive: Native Permission Prompt ---

const PERM_PROMPT_OPTIONS = [
  { label: "Yes", description: "Allow this operation", color: "emerald" },
  { label: "Yes, and always allow", description: "Don't ask again for this scope", color: "amber" },
  { label: "No", description: "Block this operation", color: "red" },
];

function PermissionPromptBubble({ item, agentId, onAnswered }) {
  const [chosenIdx, setChosenIdx] = useState(null);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState(null);

  const q = (item.questions || [])[0] || {};
  const options = q.options || PERM_PROMPT_OPTIONS;
  const isDismissed = item.answer != null && typeof item.answer === "string" &&
    (item.answer.startsWith("The user doesn't want to proceed") ||
     item.answer.startsWith("User declined") ||
     item.answer.startsWith("Tool use rejected"));

  // Resolve answered index
  const resolveIdx = () => {
    if (isDismissed) return null;
    if (item.selected_index != null) return item.selected_index;
    if (item.answer != null && typeof item.answer === "string") {
      const idx = options.findIndex((o) => (o.label || o) === item.answer);
      if (idx !== -1) return idx;
    }
    if (chosenIdx != null) return chosenIdx;
    return null;
  };

  const answeredIdx = resolveIdx();
  const isAnswered = answeredIdx != null || isDismissed || item.auto_approved;

  const handleSelect = async (idx) => {
    setChosenIdx(idx);
    setSubmitting(true);
    setError(null);
    try {
      if (item.request_id) {
        // Hook-based permission: call respondPermission API
        const decisionMap = ["allow", "allow_always", "deny"];
        await respondPermission(agentId, item.request_id, {
          decision: decisionMap[idx] || "deny",
          tool_name: item.tool_name,
        });
      } else {
        // Native CC permission prompt: send tmux keys via answerAgent
        await answerAgent(agentId, {
          tool_use_id: item.tool_use_id,
          type: "permission_prompt",
          selected_index: idx,
        });
      }
      onAnswered?.();
    } catch (e) {
      setError("Failed: " + (e.message || "Unknown error"));
      setChosenIdx(null);
    } finally {
      setSubmitting(false);
    }
  };

  // Badge
  let badgeText = null;
  let badgeClass = "";
  if (isDismissed) {
    badgeText = "Dismissed";
    badgeClass = "danger-tint-15 text-danger";
  } else if (item.auto_approved) {
    badgeText = "Auto-approved";
    badgeClass = "bg-elevated text-dim";
  } else if (item.answer === "Timed out") {
    badgeText = "Timed out";
    badgeClass = "danger-tint-15 text-danger";
  } else if (answeredIdx != null) {
    const opt = options[answeredIdx];
    const optLabel = (opt?.label || opt || "").toLowerCase();
    if (optLabel.startsWith("no") || optLabel === "deny") {
      badgeText = "Denied"; badgeClass = "danger-tint-15 text-danger";
    } else {
      badgeText = "Allowed"; badgeClass = "accent-tint-15 text-accent";
    }
  }

  return (
    <div className="mt-3 rounded-xl bg-surface p-3">
      <div className="flex items-center gap-2 mb-1.5">
        <span className="inline-block px-2 py-0.5 rounded-full accent-tint-15 text-accent text-[10px] font-semibold uppercase tracking-wider">
          Permission
        </span>
        {item.tool_name && (
          <span className="text-xs text-dim font-mono">{item.tool_name}</span>
        )}
        {badgeText && (
          <span className={`ml-auto px-2 py-0.5 rounded-full text-[10px] font-semibold uppercase tracking-wider ${badgeClass}`}>
            {badgeText}
          </span>
        )}
      </div>
      <p className="text-sm text-heading font-medium mb-2">{q.question || "Permission required"}</p>
      <div className="space-y-1.5">
        {options.map((opt, oi) => {
          const label = opt.label || opt;
          const desc = opt.description || "";
          const isChosen = answeredIdx === oi;
          const dimmed = isAnswered && !isChosen;
          const disabled = isAnswered || submitting;

          return (
            <button
              key={oi}
              type="button"
              disabled={disabled}
              onClick={() => { if (!isAnswered) handleSelect(oi); }}
              className={`w-full text-left rounded-lg px-3 py-2 text-sm transition-all border ${
                isChosen
                  ? "bg-hover border-ring-hover text-heading"
                  : dimmed
                    ? "bg-transparent border-ring-hover text-faint"
                    : "bg-transparent border-ring-hover hover:bg-elevated text-body"
              } ${disabled ? "cursor-default" : "cursor-pointer"}`}
            >
              <div className="flex items-start gap-2">
                <span className={`mt-0.5 w-4 h-4 rounded-full border-2 shrink-0 flex items-center justify-center ${
                  isChosen ? "border-accent bg-accent" : "border-ring-hover"
                }`}>
                  {isChosen && (
                    <svg className="w-2.5 h-2.5 text-white" fill="currentColor" viewBox="0 0 20 20">
                      <path fillRule="evenodd" d="M16.707 5.293a1 1 0 010 1.414l-8 8a1 1 0 01-1.414 0l-4-4a1 1 0 011.414-1.414L8 12.586l7.293-7.293a1 1 0 011.414 0z" clipRule="evenodd" />
                    </svg>
                  )}
                </span>
                <div>
                  <span className="font-medium">{label}</span>
                  {desc && <p className={`text-xs mt-0.5 ${dimmed ? "text-dim/30" : "text-dim"}`}>{desc}</p>}
                </div>
              </div>
            </button>
          );
        })}
      </div>
      {submitting && (
        <p className="text-xs text-dim mt-2 flex items-center gap-1.5">
          <span className="inline-block w-1.5 h-1.5 rounded-full bg-accent animate-pulse" />
          Sending response...
        </p>
      )}
      {error && <p className="text-xs text-red-400 mt-2 px-1">{error}</p>}
    </div>
  );
}

// --- Interactive: ExitPlanMode ---

// Must match the exact order Claude CLI shows in the ExitPlanMode TUI
// Note: "clear context" option was hidden by default in Claude Code v2.1.81
// (can be restored with showClearContextOnPlanAccept setting)
const PLAN_OPTIONS = [
  { label: "Yes, bypass permissions", description: "Approve and skip permission prompts", color: "emerald" },
  { label: "Yes, manual approval", description: "Approve but require manual approval for each edit", color: "amber" },
  { label: "Give feedback", description: "Type feedback for Claude to revise the plan", color: "indigo" },
];

function _detectPlanIdx(answer) {
  if (answer == null || typeof answer !== "string") return null;
  // Dismissed / escaped answers
  if (answer.startsWith("The user doesn't want to proceed") || answer.startsWith("User declined") || answer.startsWith("Tool use rejected")) return null;
  const a = answer.toLowerCase().trim();
  // Exact label match first (avoids keyword collision like "bypass manual")
  const exactLabels = PLAN_OPTIONS.map((o) => o.label.toLowerCase());
  const exactIdx = exactLabels.indexOf(a);
  if (exactIdx !== -1) return exactIdx;
  // Keyword fallback for answers from Claude's tool_result (may differ in wording)
  if (/bypass/.test(a) && !/manual/.test(a)) return 0;
  if (/manual/.test(a)) return 1;
  if (/feedback|type here|tell claude/.test(a)) return 2;
  if (/^yes\b/.test(a) || a === "approve" || a === "approved") return 1; // safe default: manual approval
  if (/^no\b/.test(a) || a === "reject") return 2; // feedback mode, not destructive
  return null; // unrecognized input — do not select any option
}

function _isPlanDismissed(item) {
  if (!item.answer || typeof item.answer !== "string") return false;
  return item.answer.startsWith("The user doesn't want to proceed") || item.answer.startsWith("User declined") || item.answer.startsWith("Tool use rejected");
}

function ProgressSuggestionsCard({ agentId, onApplied, onDiscarded }) {
  // Seed from cache so re-entry / refresh paints the card immediately
  // instead of returning null for the 100-300ms fetch window. Selected
  // state is re-derived from cached ids (all selected by default), same
  // as the no-cache path — no UI difference for the user.
  const cached = pendingSuggestionsCache.get(agentId);
  const [suggestions, setSuggestions] = useState(() => cached?.items || []);
  const [loading, setLoading] = useState(() => !cached);
  const [selected, setSelected] = useState(
    () => new Set((cached?.items || []).map((s) => s.id))
  );
  const [edits, setEdits] = useState({});
  const [editingId, setEditingId] = useState(null);
  const [submitting, setSubmitting] = useState(false);

  useEffect(() => {
    fetchAgentSuggestions(agentId)
      .then((data) => {
        setSuggestions(data);
        // Reset selected to "all" only when the underlying id set changed —
        // otherwise the user's checkbox toggles between cache-seed and the
        // background fetch resolution would get clobbered.
        const newIds = new Set(data.map((s) => s.id));
        setSelected((prev) => {
          const cachedIds = new Set((cached?.items || []).map((s) => s.id));
          const sameSet = newIds.size === cachedIds.size &&
            [...newIds].every((id) => cachedIds.has(id));
          return sameSet ? prev : newIds;
        });
        pendingSuggestionsCache.set(agentId, { items: data });
      })
      .catch((err) => console.error("Failed to fetch suggestions:", err))
      .finally(() => setLoading(false));
    // cached is captured at mount; intentionally not in deps to avoid
    // re-running when the cache module mutates.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [agentId]);

  const handleApply = async () => {
    setSubmitting(true);
    try {
      const accepted = [...selected].map((id) => ({
        id,
        ...(edits[id] ? { edited_content: edits[id] } : {}),
      }));
      const rejected_ids = suggestions
        .filter((s) => !selected.has(s.id))
        .map((s) => s.id);
      const res = await applyAgentSuggestions(agentId, { accepted, rejected_ids });
      // After backend commits, has_pending_suggestions=false flows through WS
      // and the parent unmounts this card → InsightsHistoryCard takes over.
      // Invalidate both caches: processed is now stale (new accepted/rejected
      // rows landed), pending is now empty (suggestions were resolved).
      processedSuggestionsCache.invalidate(agentId);
      pendingSuggestionsCache.invalidate(agentId);
      onApplied?.(res?.accepted ?? accepted.length);
    } catch (err) {
      console.error("Failed to apply suggestions:", err);
      setSubmitting(false);
    }
  };

  const handleDiscard = async () => {
    setSubmitting(true);
    try {
      await discardAgentSuggestions(agentId);
      processedSuggestionsCache.invalidate(agentId);
      pendingSuggestionsCache.invalidate(agentId);
      onDiscarded?.();
    } catch (err) {
      console.error("Failed to discard suggestions:", err);
      setSubmitting(false);
    }
  };

  if (loading) return null;
  if (!suggestions.length) return null;

  return (
    <div className="mt-4 rounded-xl bg-amber-500/10 border border-amber-500/20 p-4">
      <div className="flex items-center gap-2 mb-3">
        <svg className="w-4 h-4 text-amber-400" fill="none" stroke="currentColor" strokeWidth={2} viewBox="0 0 24 24">
          <path strokeLinecap="round" strokeLinejoin="round" d="M9.663 17h4.673M12 3v1m6.364 1.636l-.707.707M21 12h-1M4 12H3m3.343-5.657l-.707-.707m2.828 9.9a5 5 0 117.072 0l-.548.547A3.374 3.374 0 0014 18.469V19a2 2 0 11-4 0v-.531c0-.895-.356-1.754-.988-2.386l-.548-.547z" />
        </svg>
        <span className="text-sm font-medium text-amber-300">Progress Insights</span>
        <span className="text-xs text-dim ml-auto">{selected.size}/{suggestions.length} selected</span>
      </div>
      <div className="space-y-2 mb-3">
        {suggestions.map((s) => (
          <div key={s.id} className="flex items-start gap-2 group">
            <input
              type="checkbox"
              checked={selected.has(s.id)}
              onChange={() => {
                setSelected((prev) => {
                  const next = new Set(prev);
                  if (next.has(s.id)) next.delete(s.id);
                  else next.add(s.id);
                  return next;
                });
              }}
              className="mt-1 w-4 h-4 rounded border-divider bg-input text-amber-500 focus:ring-amber-500/50 shrink-0"
            />
            <div className="flex-1 min-w-0">
              {editingId === s.id ? (
                <textarea
                  autoFocus
                  rows={3}
                  value={edits[s.id] ?? s.content}
                  onChange={(e) => setEdits((prev) => ({ ...prev, [s.id]: e.target.value }))}
                  onBlur={() => setEditingId(null)}
                  onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); setEditingId(null); } }}
                  className="w-full bg-input border border-divider rounded px-2 py-1 text-sm text-body focus:outline-none focus:ring-1 focus:ring-amber-500/50 resize-y"
                />
              ) : (
                <div className="flex items-start gap-1">
                  <p
                    className="text-sm text-body leading-relaxed flex-1 min-w-0 cursor-pointer select-none"
                    onDoubleClick={() => setEditingId(s.id)}
                  >
                    {edits[s.id] ?? s.content}
                  </p>
                  <button
                    type="button"
                    onClick={() => setEditingId(s.id)}
                    className="text-dim hover:text-amber-400 transition-colors shrink-0 mt-0.5 p-0.5"
                    title="Edit"
                  >
                    <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" strokeWidth={2} viewBox="0 0 24 24">
                      <path strokeLinecap="round" strokeLinejoin="round" d="M15.232 5.232l3.536 3.536m-2.036-5.036a2.5 2.5 0 113.536 3.536L6.5 21.036H3v-3.572L16.732 3.732z" />
                    </svg>
                  </button>
                </div>
              )}
            </div>
          </div>
        ))}
      </div>
      <div className="flex gap-2">
        <button
          type="button"
          disabled={submitting || selected.size === 0}
          onClick={handleApply}
          className="px-3 py-1.5 rounded-lg bg-amber-600 hover:bg-amber-500 text-white text-sm font-semibold transition-colors disabled:opacity-50"
        >
          {submitting ? "Applying..." : `Apply Selected (${selected.size})`}
        </button>
        <button
          type="button"
          disabled={submitting}
          onClick={handleDiscard}
          className="px-3 py-1.5 rounded-lg bg-input hover:bg-elevated text-body text-sm transition-colors"
        >
          Discard All
        </button>
      </div>
    </div>
  );
}

function InsightStatusCard({ status, agentId, onRetry }) {
  const [submitting, setSubmitting] = useState(false);

  const handleRetry = async () => {
    setSubmitting(true);
    try {
      await regenerateAgentInsights(agentId);
      onRetry?.();
    } catch (err) {
      console.error("Failed to regenerate insights:", err);
    } finally {
      setSubmitting(false);
    }
  };

  if (status === "generating") {
    return (
      <div className="mt-4 rounded-xl bg-blue-500/10 border border-blue-500/20 p-3 flex items-center gap-2">
        <svg className="w-4 h-4 text-blue-400/60 animate-spin" fill="none" viewBox="0 0 24 24">
          <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
          <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z" />
        </svg>
        <span className="text-sm text-blue-300/70">Generating insights...</span>
      </div>
    );
  }

  return (
    <div className="mt-4 rounded-xl bg-red-500/10 border border-red-500/20 p-3 flex items-center justify-between">
      <div className="flex items-center gap-2">
        <svg className="w-4 h-4 text-red-400/60" fill="none" stroke="currentColor" strokeWidth={2} viewBox="0 0 24 24">
          <path strokeLinecap="round" strokeLinejoin="round" d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-2.5L13.732 4.5c-.77-.833-2.694-.833-3.464 0L3.34 16.5c-.77.833.192 2.5 1.732 2.5z" />
        </svg>
        <span className="text-sm text-red-300/70">Insight generation failed</span>
      </div>
      <button
        type="button"
        onClick={handleRetry}
        disabled={submitting}
        className="text-xs px-2.5 py-1 rounded-lg bg-red-500/20 text-red-300/80 hover:bg-red-500/30 transition-colors disabled:opacity-50"
      >
        {submitting ? "Retrying..." : "Retry"}
      </button>
    </div>
  );
}

function InsightsHistoryCard({ agentId }) {
  // Seed from cache so re-entry / refresh paints the collapsed card
  // immediately. Empty cache → fall back to the "Loading insights…"
  // shimmer below until the first fetch resolves.
  const cached = processedSuggestionsCache.get(agentId);
  const [items, setItems] = useState(() => cached?.items || []);
  const [expanded, setExpanded] = useState(false);
  const [loaded, setLoaded] = useState(() => !!cached);

  useEffect(() => {
    fetchProcessedSuggestions(agentId)
      .then((data) => {
        setItems(data);
        setLoaded(true);
        processedSuggestionsCache.set(agentId, { items: data });
      })
      .catch(() => setLoaded(true));
  }, [agentId]);

  // Loaded with zero items → genuinely no history to show. Hide entirely.
  if (loaded && items.length === 0) return null;

  // Render the chrome immediately so the bubble doesn't blink during the
  // ~100-300ms fetch (e.g. right after Apply, when ProgressSuggestionsCard
  // unmounts and we mount). Inner content fills in once fetch resolves.
  if (!loaded) {
    return (
      <div className="mt-4 rounded-xl bg-amber-500/10 border border-amber-500/20 p-3">
        <div className="flex items-center gap-2">
          <svg className="w-4 h-4 text-amber-400/60 animate-pulse" fill="none" stroke="currentColor" strokeWidth={2} viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" d="M9.663 17h4.673M12 3v1m6.364 1.636l-.707.707M21 12h-1M4 12H3m3.343-5.657l-.707-.707m2.828 9.9a5 5 0 117.072 0l-.548.547A3.374 3.374 0 0014 18.469V19a2 2 0 11-4 0v-.531c0-.895-.356-1.754-.988-2.386l-.548-.547z" />
          </svg>
          <span className="text-sm text-amber-300/50">Loading insights…</span>
        </div>
      </div>
    );
  }

  const accepted = items.filter((s) => s.status === "accepted");
  const rejected = items.filter((s) => s.status === "rejected");
  const allDiscarded = accepted.length === 0;

  return (
    <div className="mt-4 rounded-xl bg-amber-500/10 border border-amber-500/20 p-3">
      <button
        type="button"
        className="flex items-center gap-2 w-full text-left"
        onClick={() => setExpanded((v) => !v)}
      >
        <svg className="w-4 h-4 text-amber-400/60" fill="none" stroke="currentColor" strokeWidth={2} viewBox="0 0 24 24">
          <path strokeLinecap="round" strokeLinejoin="round" d="M9.663 17h4.673M12 3v1m6.364 1.636l-.707.707M21 12h-1M4 12H3m3.343-5.657l-.707-.707m2.828 9.9a5 5 0 117.072 0l-.548.547A3.374 3.374 0 0014 18.469V19a2 2 0 11-4 0v-.531c0-.895-.356-1.754-.988-2.386l-.548-.547z" />
        </svg>
        <span className="text-sm text-amber-300/70 font-medium">
          {allDiscarded
            ? `Insights discarded (${rejected.length})`
            : `${accepted.length} insight${accepted.length !== 1 ? "s" : ""} applied`}
          {!allDiscarded && rejected.length > 0 && (
            <span className="text-dim font-normal ml-1">
              ({rejected.length} discarded)
            </span>
          )}
        </span>
        <svg
          className={`w-3.5 h-3.5 text-dim ml-auto transition-transform ${expanded ? "rotate-180" : ""}`}
          fill="none" stroke="currentColor" strokeWidth={2} viewBox="0 0 24 24"
        >
          <path strokeLinecap="round" strokeLinejoin="round" d="M19 9l-7 7-7-7" />
        </svg>
      </button>
      {expanded && (
        <div className="mt-2 space-y-1 pl-6">
          {accepted.map((s) => (
            <p key={s.id} className="text-sm text-body/70 leading-relaxed flex items-start gap-1.5">
              <svg className="w-3.5 h-3.5 text-ok opacity-60 mt-0.5 shrink-0" fill="none" stroke="currentColor" strokeWidth={2} viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" d="M5 13l4 4L19 7" />
              </svg>
              {s.edited_content || s.content}
            </p>
          ))}
          {rejected.map((s) => (
            <p key={s.id} className="text-sm text-dim/50 leading-relaxed line-through flex items-start gap-1.5">
              <svg className="w-3.5 h-3.5 text-red-400/40 mt-0.5 shrink-0" fill="none" stroke="currentColor" strokeWidth={2} viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" d="M6 18L18 6M6 6l12 12" />
              </svg>
              {s.content}
            </p>
          ))}
        </div>
      )}
    </div>
  );
}

function PlanBubble({ item, agentId, onAnswered }) {
  const [chosenIdx, setChosenIdx] = useState(null);
  const [submitting, setSubmitting] = useState(false);
  const [planExpanded, setPlanExpanded] = useState(true);
  const [planError, setPlanError] = useState(null);

  const isDismissed = _isPlanDismissed(item);
  // Determine the effective selected index: stored index > answer parse > local choice
  // When dismissed, do NOT fall back to index detection — no option should highlight
  const serverIdx = isDismissed ? null : (item.selected_index ?? (item.answer != null ? _detectPlanIdx(item.answer) : null));
  const effectiveIdx = isDismissed ? null : (serverIdx ?? chosenIdx);
  const isAnswered = effectiveIdx != null || isDismissed || item.auto_approved;

  // Plan content comes directly from metadata (extracted from ExitPlanMode tool_input)
  const planContent = item.plan || null;

  const handleSelect = async (idx) => {
    setChosenIdx(idx);
    setSubmitting(true);
    setPlanError(null);
    try {
      await answerAgent(agentId, {
        tool_use_id: item.tool_use_id,
        type: "exit_plan_mode",
        selected_index: idx,
      });
      onAnswered?.();
    } catch (e) {
      console.error("Failed to answer plan:", e);
      setPlanError("Failed to send plan response: " + (e.message || "Unknown error"));
      setChosenIdx(null); // revert on failure
    } finally {
      setSubmitting(false);
    }
  };

  // Badge
  let badgeText = null;
  let badgeClass = "";
  if (isDismissed) {
    badgeText = "Dismissed";
    badgeClass = "danger-tint-15 text-danger";
  } else if (item.auto_approved) {
    badgeText = "Auto-approved";
    badgeClass = "bg-elevated text-dim";
  } else if (effectiveIdx != null) {
    badgeText = "Choice Sent";
    badgeClass = "accent-tint-15 text-accent";
  }

  return (
    <div className="mt-3 rounded-xl bg-surface p-3">
      <div className="flex items-center gap-2 mb-2">
        <span className="inline-block px-2 py-0.5 rounded-full accent-tint-15 text-accent text-[10px] font-semibold uppercase tracking-wider">
          Plan Approval
        </span>
        {planContent && (
          <button
            type="button"
            onClick={() => setPlanExpanded((v) => !v)}
            className="text-[10px] text-dim hover:text-body transition-colors"
          >
            {planExpanded ? "Hide plan" : "Show plan"}
          </button>
        )}
        {badgeText && (
          <span className={`ml-auto px-2 py-0.5 rounded-full text-[10px] font-semibold uppercase tracking-wider ${badgeClass}`}>
            {badgeText}
          </span>
        )}
      </div>
      {/* Inline plan content */}
      {planContent && planExpanded && (
        <div className="mb-3 rounded-lg bg-input overflow-hidden">
          <div className="px-3 py-2 max-h-[300px] overflow-y-auto text-sm">
            <SafeMarkdown fallback={planContent}>
              <div className="prose-sm text-body [&_h1]:text-base [&_h1]:font-semibold [&_h1]:text-heading [&_h1]:mt-3 [&_h1]:mb-1.5 [&_h2]:text-sm [&_h2]:font-semibold [&_h2]:text-heading [&_h2]:mt-2.5 [&_h2]:mb-1 [&_h3]:text-xs [&_h3]:font-semibold [&_h3]:text-heading [&_h3]:mt-2 [&_h3]:mb-1 [&_p]:text-xs [&_p]:mb-1.5 [&_ul]:text-xs [&_ul]:ml-4 [&_ul]:mb-1.5 [&_ol]:text-xs [&_ol]:ml-4 [&_ol]:mb-1.5 [&_li]:mb-0.5 [&_code]:text-[11px] [&_code]:bg-elevated [&_code]:px-1 [&_code]:rounded [&_pre]:text-[11px] [&_pre]:bg-elevated [&_pre]:p-2 [&_pre]:rounded [&_pre]:overflow-x-auto [&_pre]:mb-2">
                {renderMarkdown(planContent)}
              </div>
            </SafeMarkdown>
          </div>
        </div>
      )}
      <div className="space-y-1.5">
        {PLAN_OPTIONS.map((opt, oi) => {
          const isChosen = effectiveIdx === oi;
          const dimmed = isAnswered && !isChosen;

          return (
            <button
              key={oi}
              type="button"
              disabled={isAnswered || submitting}
              onClick={() => !isAnswered && handleSelect(oi)}
              className={`w-full text-left rounded-lg px-3 py-2 text-sm transition-all border ${
                isChosen
                  ? "bg-hover border-ring-hover text-heading"
                  : dimmed
                    ? "bg-transparent border-ring-hover text-faint"
                    : "bg-transparent border-ring-hover hover:bg-elevated text-body"
              } ${isAnswered ? "cursor-default" : "cursor-pointer"}`}
            >
              <div className="flex items-start gap-2">
                <span className={`mt-0.5 w-4 h-4 rounded-full border-2 shrink-0 flex items-center justify-center ${
                  isChosen ? "border-accent bg-accent" : "border-ring-hover"
                }`}>
                  {isChosen && (
                    <svg className="w-2.5 h-2.5 text-white" fill="currentColor" viewBox="0 0 20 20">
                      <path fillRule="evenodd" d="M16.707 5.293a1 1 0 010 1.414l-8 8a1 1 0 01-1.414 0l-4-4a1 1 0 011.414-1.414L8 12.586l7.293-7.293a1 1 0 011.414 0z" clipRule="evenodd" />
                    </svg>
                  )}
                </span>
                <div>
                  <span className="font-medium">{opt.label}</span>
                  <p className={`text-xs mt-0.5 ${dimmed ? "text-dim/30" : "text-dim"}`}>{opt.description}</p>
                </div>
              </div>
            </button>
          );
        })}
      </div>
      {submitting && (
        <p className="text-xs text-dim mt-2 flex items-center gap-1.5">
          <span className="inline-block w-1.5 h-1.5 rounded-full bg-accent animate-pulse" />
          Sending response...
        </p>
      )}
      {planError && (
        <p className="text-xs text-red-400 mt-2 px-1">{planError}</p>
      )}
    </div>
  );
}

// --- Interactive items renderer ---

function InteractiveBubbles({ metadata, agentId, onAnswered, messageContent, project }) {
  if (!metadata?.interactive?.length) return null;
  return metadata.interactive.map((item) => {
    if (item.type === "ask_user_question") {
      return <QuestionBubble key={item.tool_use_id} item={item} agentId={agentId} onAnswered={onAnswered} />;
    }
    if (item.type === "exit_plan_mode") {
      return <PlanBubble key={item.tool_use_id} item={item} agentId={agentId} onAnswered={onAnswered} />;
    }
    if (item.type === "permission_prompt") {
      return <PermissionPromptBubble key={item.tool_use_id} item={item} agentId={agentId} onAnswered={onAnswered} />;
    }
    // Unknown interactive type — render a generic informational card
    return (
      <div key={item.tool_use_id} className="mt-3 rounded-xl bg-surface/60 border border-divider/40 p-3">
        <div className="flex items-center gap-2 mb-1">
          <span className="text-sm font-medium text-dim">Interactive prompt</span>
          <span className="ml-auto px-2 py-0.5 rounded-full text-[10px] font-semibold uppercase tracking-wider bg-dim/20 text-dim">
            {item.type}
          </span>
        </div>
        {item.answer && <p className="text-xs text-body">{item.answer}</p>}
      </div>
    );
  });
}

// Regex matching tool summary lines in message content: > `ToolName` ...
const TOOL_SUMMARY_RE = /^> `(\w+)`\s*(.*)/;

/**
 * Split agent message content into interleaved text + tool segments.
 * Uses `> ToolName` summary lines in the content as delimiters.
 * Returns [{type:"text",text}, {type:"tools",entries}, ...].
 */
function splitMessageSegments(content) {
  const lines = content.split("\n");
  const result = [];
  let text = [], tools = [];
  for (const line of lines) {
    const m = line.match(TOOL_SUMMARY_RE);
    if (m) {
      if (text.length) { const t = text.join("\n").trim(); if (t) result.push({ type: "text", text: t }); text = []; }
      tools.push({ name: m[1], summary: m[2] });
    } else {
      if (tools.length) { result.push({ type: "tools", entries: [...tools] }); tools = []; }
      text.push(line);
    }
  }
  if (tools.length) result.push({ type: "tools", entries: tools });
  const t = text.join("\n").trim();
  if (t) result.push({ type: "text", text: t });
  return result;
}

/** Lightweight agent text bubble for non-final segments (no timestamp / actions). */
// Record equality for the 3 s agent poll: an unchanged payload keeps the
// previous object, so `agent` consumers (and the whole page) don't re-render.
function sameAgentRecord(a, b) {
  if (!a || !b) return false;
  const ka = Object.keys(a);
  const kb = Object.keys(b);
  if (ka.length !== kb.length) return false;
  for (const k of kb) {
    const x = a[k];
    const y = b[k];
    if (x === y) continue;
    if (x && y && typeof x === "object" && typeof y === "object"
        && JSON.stringify(x) === JSON.stringify(y)) continue;
    return false;
  }
  return true;
}

function AgentTextSegment({ text, project }) {
  return (
    <div className="flex justify-start my-2">
      <div className="max-w-[min(85%,30rem)] min-w-0">
        <div className="rounded-2xl px-4 py-2.5 bg-surface shadow-card text-body rounded-bl-md overflow-hidden">
          <div className="text-sm break-words chat-bubble-content">
            <SafeMarkdown fallback={text}>
              {renderMarkdown(text, project)}
            </SafeMarkdown>
          </div>
        </div>
      </div>
    </div>
  );
}

function ChatBubble({ message, project, onCancelMessage, onUpdateMessage, onSendNow, onStartEdit, editingMessageId, agentId, onRefresh, queuePosition, queueTotal, contentOverride, toolEntries, openMenuMsgId, setOpenMenuMsgId, bookmarkedSet, onAfterBookmark }) {
  if (message.kind === "tool_activity") {
    return null;
  }
  if (message.role === "SYSTEM") {
    if (message.metadata?.stop_action || message.content.startsWith("Task dropped") || message.content.startsWith("Redo")) {
      return <StopNoteCard message={message} />;
    }
    return <SystemBubble message={message} />;
  }
  // Probe-fired wake messages are USER role on the wire (must go via tmux),
  // but visually they belong with /loop wakeup and other system events —
  // they are external triggers, not user input. Render as SystemBubble.
  // Identity carrier is metadata.probe_id; legacy rows (created before the
  // probe→web source merge) used source==="probe" — keep both for back-compat.
  if (message.metadata?.probe_id || message.source === "probe") {
    return <SystemBubble message={message} />;
  }

  // Sub-agent task notifications get their own collapsible bubble
  if (message.metadata?.task_notification || (message.content || "").trimStart().startsWith("<task-notification>")) {
    return <SubAgentBubble message={message} project={project} />;
  }

  // Web app cards posted via the MCP webapp_present tool
  if (message.kind === "webapp" && message.metadata?.webapp) {
    return <WebAppCardBubble message={message} agentId={agentId} />;
  }

  const isUser = message.role === "USER";
  // Normalize status for comparison: the Phase 2 refactor introduces
  // lowercase pre-sent statuses ('queued', 'scheduled', 'cancelled',
  // 'sent', 'delivered', 'executed') alongside the uppercase MessageStatus
  // enum values. Match on both — pre-sent zone uses lowercase, DB-backed
  // messages use uppercase.
  const status = message.status || "";
  const isScheduled = isUser && message.scheduled_at && status === "scheduled";
  const isPreQueued = isUser && !message.scheduled_at && status === "queued";
  const isCancelled = isUser && (status === "cancelled" || status === "CANCELLED");
  const isSent = isUser && (status === "sent" || status === "SENT");
  const isExecuted = isUser && status === "executed";
  // Legacy aliases still used throughout the render below.
  const isPending = isPreQueued || isSent;
  const isQueued = isSent;
  const isSlashCommand = isUser && (message.content || "").trimStart().startsWith("/");
  const isWebUndelivered = isUser && message.source === "web" && !message.delivered_at && !isPending && !isScheduled && !isQueued && !isSlashCommand && !isCancelled;
  const UNDELIVERED_TIMEOUT_MS = 5 * 60 * 1000; // 5 minutes
  const isUndeliveredTimedOut = isWebUndelivered && (serverNow() - new Date(message.created_at).getTime()) > UNDELIVERED_TIMEOUT_MS;

  // Action menu visibility is hoisted to the parent so only one menu can be
  // open across the whole message list at any time. Falls back to local state
  // when the parent didn't wire props (e.g. legacy/embedded callers).
  const [localShowActions, setLocalShowActions] = useState(false);
  const showActions = setOpenMenuMsgId
    ? openMenuMsgId === message.id
    : localShowActions;
  const setShowActions = (open) => {
    if (setOpenMenuMsgId) {
      setOpenMenuMsgId(open ? message.id : (openMenuMsgId === message.id ? null : openMenuMsgId));
    } else {
      setLocalShowActions(open);
    }
  };
  const [copied, setCopied] = useState(false);
  const [inlineLightbox, setInlineLightbox] = useState(null); // { media, initialIndex }
  const [showDiverge, setShowDiverge] = useState(false);
  const bookmarkActive = !!(bookmarkedSet && message?.id && bookmarkedSet.has(message.id));
  const bubbleToast = useToast();
  const navigate = useNavigate();
  const lastTapRef = useRef(0);
  const touchStartYRef = useRef(0);
  const markdownRef = useRef(null);
  // This bubble's content is currently loaded in the composer for editing.
  const isBeingEdited = !!editingMessageId && editingMessageId === message.id;

  // Handle click on inline markdown images to open lightbox
  const handleMarkdownClick = useCallback((e) => {
    const img = e.target.closest("img");
    if (!img) return;
    const container = markdownRef.current;
    if (!container) return;
    const allImgs = Array.from(container.querySelectorAll("img"));
    if (allImgs.length === 0) return;
    const index = allImgs.indexOf(img);
    const media = allImgs.map((el) => ({ type: "image", src: el.src, filename: el.alt || "" }));
    setInlineLightbox({ media, initialIndex: Math.max(0, index) });
  }, []);

  // Action menu visibility rules for the two-stage delete model:
  //   queued/scheduled → Copy / Modify / Delete (+ Send Now for scheduled)
  //   cancelled        → Copy / Delete (hard tombstone)
  //   sent+            → Copy only
  const isPreDelivery = isPreQueued || isScheduled || isCancelled;
  const canEdit = isPreQueued || isScheduled; // controls Modify button
  const canDelete = isPreQueued || isScheduled || isCancelled;
  const canSendNow = isScheduled;
  // Bookmark eligibility: any settled (delivered/executed) USER message or any
  // AGENT message. Skip pre-delivery (queued/scheduled/cancelled) and SYSTEM/
  // task-notification bubbles which already exited above.
  const canBookmark = !isPreDelivery && !!message.id && (message.role === "USER" || message.role === "AGENT");
  // Diverge eligibility: settled USER/AGENT text messages that came through
  // the JSONL (backend re-validates the session anchor). Interactive cards
  // and non-text kinds have no clean fork point.
  const canDiverge = canBookmark
    && (!message.kind || message.kind === "text")
    && !message.metadata?.interactive?.length;
  // `canModify` gates the action popover opening at all — anything with
  // any action button (including Copy-only) should still show the menu,
  // but keeping the name for minimal diff: open the popover for any
  // pre-sent state + any user message (Copy-only path).
  const canModify = canEdit; // legacy name: controls edit-path UI only

  // Touch start — only used to track the start Y so we can distinguish a
  // tap from a scroll in handleTouchEnd. The action menu is opened by
  // double-tap only (no long-press).
  const handleTouchStart = (e) => {
    touchStartYRef.current = e.touches?.[0]?.clientY ?? 0;
  };

  const handleBookmark = async (e) => {
    e?.stopPropagation?.();
    setShowActions(false);
    if (!project || !message?.id) return;
    try {
      if (bookmarkActive) {
        await deleteBookmark(project, message.id);
        onAfterBookmark?.(message.id, "removed");
        bubbleToast.success("Bookmark removed");
      } else {
        await createBookmark(project, message.id);
        onAfterBookmark?.(message.id, "added");
      }
    } catch (err) {
      bubbleToast.error(err?.message || "Failed to update bookmark");
    }
  };
  const handleDivergeOpen = (e) => {
    e?.stopPropagation?.();
    setShowActions(false);
    setShowDiverge(true);
  };
  const handleDiverged = (agent, purpose) => {
    setShowDiverge(false);
    // Prefill the new chat's composer via its localStorage draft key —
    // useDraft treats localStorage as the source of truth on mount.
    // USER-message diverge = edit-and-resend: seed with the original text;
    // AGENT-message diverge: seed with the entered purpose (if any).
    const prefill = isUser ? (displayContent || "") : purpose;
    if (prefill) {
      try { safeSetItem(`draft:chat:${agent.id}`, prefill); } catch { /* ignore */ }
    }
    bubbleToast.success("Diverged");
    navigate(`/agents/${agent.id}`);
  };
  // Touch end — double-tap detection. Skip if the finger moved significantly
  // (scroll gesture, not a tap).
  const handleTouchEnd = (e) => {
    const endY = e.changedTouches?.[0]?.clientY ?? 0;
    const movedTooFar = Math.abs(endY - touchStartYRef.current) > 10;
    if (!movedTooFar) {
      const now = Date.now();
      if (now - lastTapRef.current < DOUBLE_TAP_WINDOW) {
        handleDoubleClick();
      }
      lastTapRef.current = now;
    }
  };
  const handleDoubleClick = () => {
    // Double-tap / double-click always opens the action menu when any action
    // is available. Direct-copy was removed — Copy lives inside the menu.
    if (canEdit || isCancelled || canBookmark) {
      setShowActions(true);
    }
  };

  const handleCopy = () => {
    navigator.clipboard.writeText(message.content || "").then(() => {
      setCopied(true);
      setTimeout(() => setCopied(false), COPY_TOAST_DURATION);
    }).catch(() => {});
    setShowActions(false);
  };

  const handleCancel = () => {
    setShowActions(false);
    onCancelMessage?.(message.id);
  };
  const handleSendNow = () => {
    setShowActions(false);
    onSendNow?.(message.id);
  };
  // Modify → hand the message to the composer (edit-in-input-bar). The
  // bottom bar sits above the mobile keyboard (bottom: var(--kb-h)), so
  // the text stays visible while editing — unlike the old inline-bubble
  // textarea, which the keyboard covered on phones.
  const handleEdit = () => {
    setShowActions(false);
    onStartEdit?.(message);
  };

  const attachments = useMemo(() => {
    const base = extractFileAttachments(message.content, project, message.role, message.metadata);
    if (!toolEntries?.length) return base;
    // Extract media paths from preceding tool entry summaries (Read/Edit/Write paths)
    const toolText = toolEntries.map(e => e.summary || "").join("\n");
    if (!toolText) return base;
    const toolFiles = extractFileAttachments(toolText, project, "AGENT", null);
    if (!toolFiles.length) return base;
    const seen = new Set(base.map(a => a.path));
    return [...base, ...toolFiles.filter(f => !seen.has(f.path))];
  }, [message.content, project, message.role, message.metadata, toolEntries]);

  // Display content: use contentOverride for interleaved mode, otherwise
  // use message.content directly (backend handles attachment stripping).
  // Fallback: strip tags for old display files without metadata.attachments.
  const displayContent = useMemo(() => {
    if (contentOverride != null) return contentOverride;
    if (isUser && !message.metadata?.attachments) return stripAttachmentTags(message.content);
    return message.content;
  }, [contentOverride, isUser, message.content, message.metadata]);

  // Scheduled bubble shows a "due in …" relative countdown (matches the
  // deferred_to display in AgentRow / InboxCard) — absolute time is in the
  // tooltip so users can still verify.
  const scheduledRelative = isScheduled ? relativeTime(message.scheduled_at) : null;
  const scheduledAbsolute = isScheduled
    ? new Date(message.scheduled_at).toLocaleString([], DATE_SHORT)
    : null;

  // Picker for the "change time" button in the editing popover (scheduled msgs).
  const [showEditPicker, setShowEditPicker] = useState(false);

  const userInsights = isUser ? message.metadata?.insights : null;

  return (
    <div className={`flex ${isUser ? "justify-end" : "justify-start"} my-2`}>
      <div className={`max-w-[min(85%,30rem)] min-w-0 relative ${isUser ? "flex flex-col items-end" : ""}`}>
        <div className={isUser ? "flex items-center gap-2 max-w-full" : undefined}>
        {isUndeliveredTimedOut && (
          <div className="flex-shrink-0 text-red-400" title="Message not delivered — Claude may not have received this">
            <svg className="w-5 h-5" fill="none" stroke="currentColor" strokeWidth={2} viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" d="M12 9v4m0 4h.01M12 2L2 20h20L12 2z" />
            </svg>
          </div>
        )}
        <div
          className={`rounded-2xl px-4 py-2.5 ${
            isUser
              ? isCancelled
                ? "bg-gray-400 text-white rounded-br-md"
                : isScheduled
                  ? "bg-amber-600/80 text-white rounded-br-md"
                  : isPreQueued
                    ? "bg-bubble-60 text-bubble-ink-80 rounded-br-md"
                    : isUndeliveredTimedOut
                      ? "bg-red-600/40 text-white/70 rounded-br-md"
                      : "bg-bubble text-bubble-ink rounded-br-md"
              : "bg-surface shadow-card text-body rounded-bl-md"
          } ${canModify ? "select-none" : ""} ${isBeingEdited ? "ring-2 ring-white/70" : ""} overflow-hidden`}
          onDoubleClick={handleDoubleClick}
          onTouchStart={handleTouchStart}
          onTouchEnd={handleTouchEnd}
          onTouchCancel={handleTouchEnd}
        >
          {isUser ? (
            displayContent && (
              <div className="text-sm user-md break-words chat-bubble-content">
                <SafeMarkdown fallback={displayContent}>
                  {renderMarkdown(displayContent, project)}
                </SafeMarkdown>
              </div>
            )
          ) : (
            <div className="text-sm break-words chat-bubble-content" ref={markdownRef} onClick={handleMarkdownClick}>
              <SafeMarkdown fallback={displayContent}>
                {renderMarkdown(displayContent, project)}
              </SafeMarkdown>
            </div>
          )}
          <div className={`text-xs mt-1 flex items-center gap-1.5 ${
            isUser
              ? isCancelled ? "text-gray-100" : isScheduled ? "text-amber-200" : "text-bubble-dim"
              : "text-dim"
          }`}>
            {isScheduled ? (
              <span className="inline-flex items-center gap-1" title={scheduledAbsolute}>
                <svg className="w-3 h-3" fill="none" stroke="currentColor" strokeWidth={2} viewBox="0 0 24 24">
                  <circle cx="12" cy="12" r="10" />
                  <path strokeLinecap="round" d="M12 6v6l4 2" />
                </svg>
                {scheduledRelative}
              </span>
            ) : (
              relativeTime(message.completed_at || message.created_at)
            )}
            {isCancelled && (
              <span className="text-gray-200/90">cancelled</span>
            )}
            {!isCancelled && isPreQueued && (
              <span className="text-accent-70">
                {queueTotal > 1 ? `queued (${queuePosition} of ${queueTotal})` : "queued"}
              </span>
            )}
            {message.source && (
              <span className={`px-1 py-0.5 rounded text-[10px] font-medium leading-none ${
                isCancelled
                  ? "bg-gray-500/40 text-gray-100"
                  : message.source === "web"
                    ? isUser
                      ? "bg-white/20 text-bubble-ink"
                      : "accent-tint-20 text-accent"
                    : isUser
                      ? "bg-white/20 text-bubble-ink"
                      : "bg-emerald-500/20 text-emerald-700 dark:text-emerald-300"
              }`}>
                {message.source}
              </span>
            )}
            {message.status === "FAILED" && (
              <span className="text-red-400" title={message.error_message || ""}>Failed</span>
            )}
            {message.status === "TIMEOUT" && (
              <span className="text-orange-600 dark:text-orange-400">Timed out</span>
            )}
            {/* Check icon: three-state delivery indicator (skipped for
                pre-sent states — queued/scheduled/cancelled — and
                slash commands which share the same state machine but have
                their own executed double-check below). */}
            {isUser && message.source === "web" && !isPreDelivery && (isSlashCommand ? (
              (message.delivered_at || status === "delivered" || isExecuted) && (
                (message.completed_at || isExecuted) ? (
                  <span className="ml-auto text-ok" title={`Executed ${message.completed_at ? new Date(message.completed_at).toLocaleTimeString() : ""}`}>
                    <svg className="w-4 h-3.5" fill="none" stroke="currentColor" strokeWidth={2.5} viewBox="0 0 28 24">
                      <path strokeLinecap="round" strokeLinejoin="round" d="M2 13l4 4L16 7" />
                      <path strokeLinecap="round" strokeLinejoin="round" d="M10 13l4 4L24 7" />
                    </svg>
                  </span>
                ) : (
                  <span className="ml-auto text-accent-60" title="Received — executing...">
                    <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" strokeWidth={2.5} viewBox="0 0 24 24">
                      <path strokeLinecap="round" strokeLinejoin="round" d="M5 13l4 4L19 7" />
                    </svg>
                  </span>
                )
              )
            ) : isExecuted ? (
              <span className="ml-auto text-ok" title={`Executed ${message.completed_at ? new Date(message.completed_at).toLocaleTimeString() : ""}`}>
                <svg className="w-4 h-3.5" fill="none" stroke="currentColor" strokeWidth={2.5} viewBox="0 0 28 24">
                  <path strokeLinecap="round" strokeLinejoin="round" d="M2 13l4 4L16 7" />
                  <path strokeLinecap="round" strokeLinejoin="round" d="M10 13l4 4L24 7" />
                </svg>
              </span>
            ) : status === "delivered" || message.delivered_at ? (
              <span className="ml-auto text-ok" title={`Delivered ${message.delivered_at ? new Date(message.delivered_at).toLocaleTimeString() : ""}`}>
                <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" strokeWidth={2.5} viewBox="0 0 24 24">
                  <path strokeLinecap="round" strokeLinejoin="round" d="M5 13l4 4L19 7" />
                </svg>
              </span>
            ) : status === "sent" || isSent ? (
              <span className="ml-auto text-gray-100/80" title="Sent">
                <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" strokeWidth={2.5} viewBox="0 0 24 24">
                  <path strokeLinecap="round" strokeLinejoin="round" d="M5 13l4 4L19 7" />
                </svg>
              </span>
            ) : isUndeliveredTimedOut ? (
              <span className="ml-auto text-red-400" title="Undelivered">
                <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" strokeWidth={2.5} viewBox="0 0 24 24">
                  <path strokeLinecap="round" strokeLinejoin="round" d="M6 18L18 6M6 6l12 12" />
                </svg>
              </span>
            ) : (
              <span className="ml-auto text-accent-40" title="Sending...">
                <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" strokeWidth={2.5} viewBox="0 0 24 24">
                  <path strokeLinecap="round" strokeLinejoin="round" d="M5 13l4 4L19 7" opacity="0.5" />
                </svg>
              </span>
            ))}
          </div>
        </div>
        </div>
        {message.status === "FAILED" && message.error_message && (
          <p className="text-xs text-red-400/70 mt-1 px-1">{message.error_message}</p>
        )}
        {copied && (
          <div className="fixed inset-0 flex items-center justify-center pointer-events-none z-[9999]">
            <div className="bg-black/80 text-white text-sm font-medium px-4 py-2 rounded-xl shadow-lg">
              Copied
            </div>
          </div>
        )}
        {userInsights && userInsights.length > 0 && (
          <InsightsBubble insights={userInsights} />
        )}
        {attachments.length > 0 && <FileAttachments attachments={attachments} compact={message.role === "USER"} />}
        {inlineLightbox && (
          <ImageLightbox
            media={inlineLightbox.media}
            initialIndex={inlineLightbox.initialIndex}
            onClose={() => setInlineLightbox(null)}
          />
        )}
        {showDiverge && (
          <DivergePrompt
            agentId={agentId}
            message={message}
            onClose={() => setShowDiverge(false)}
            onDiverged={handleDiverged}
          />
        )}
        {!isUser && message.metadata?.interactive?.length > 0 && (
          <InteractiveBubbles metadata={message.metadata} agentId={agentId} onAnswered={onRefresh} messageContent={message.content} project={project} />
        )}
        {/* Action popover — gated by message status (two-stage delete model). */}
        {showActions && (
          <div className="absolute top-0 right-0 -translate-y-full mb-1 z-50">
            <div className="bg-surface border border-divider rounded-xl shadow-lg overflow-hidden flex">
              {canSendNow && (
                <button
                  type="button"
                  onClick={handleSendNow}
                  title="Send now"
                  className="px-3 py-2 text-emerald-400 hover:bg-emerald-600/10 transition-colors"
                >
                  <svg className="w-4 h-4" fill="none" stroke="currentColor" strokeWidth={2} viewBox="0 0 24 24">
                    <path strokeLinecap="round" strokeLinejoin="round" d="M6 12L3.269 3.126A59.768 59.768 0 0121.485 12 59.77 59.77 0 013.27 20.876L5.999 12zm0 0h7.5" />
                  </svg>
                </button>
              )}
              {isScheduled && (
                <div className="relative">
                  <button
                    type="button"
                    onClick={() => setShowEditPicker(v => !v)}
                    title={`Reschedule (${new Date(message.scheduled_at).toLocaleString([], DATE_SHORT)})`}
                    className="px-3 py-2 text-amber-400 hover:bg-amber-600/10 transition-colors"
                  >
                    <svg className="w-4 h-4" fill="none" stroke="currentColor" strokeWidth={2} viewBox="0 0 24 24">
                      <path strokeLinecap="round" strokeLinejoin="round" d="M8 7V3m8 4V3m-9 8h10M5 21h14a2 2 0 002-2V7a2 2 0 00-2-2H5a2 2 0 00-2 2v12a2 2 0 002 2z" />
                    </svg>
                  </button>
                  {showEditPicker && (
                    <SendLaterPicker
                      title="Schedule At"
                      onSelect={(iso) => {
                        onUpdateMessage?.(message.id, { scheduled_at: iso });
                        setShowEditPicker(false);
                        setShowActions(false);
                      }}
                      onClose={() => setShowEditPicker(false)}
                    />
                  )}
                </div>
              )}
              <button
                type="button"
                onClick={handleCopy}
                title="Copy"
                className="px-3 py-2 text-heading hover:bg-input transition-colors"
              >
                <svg className="w-4 h-4" fill="none" stroke="currentColor" strokeWidth={2} viewBox="0 0 24 24">
                  <path strokeLinecap="round" strokeLinejoin="round" d="M8 16H6a2 2 0 01-2-2V6a2 2 0 012-2h8a2 2 0 012 2v2m-6 12h8a2 2 0 002-2v-8a2 2 0 00-2-2h-8a2 2 0 00-2 2v8a2 2 0 002 2z" />
                </svg>
              </button>
              {canBookmark && (
                <button
                  type="button"
                  onClick={handleBookmark}
                  title={bookmarkActive ? "Bookmarked" : "Bookmark"}
                  className="px-3 py-2 text-amber-500 hover:bg-amber-600/10 transition-colors"
                >
                  <svg className="w-4 h-4" fill={bookmarkActive ? "currentColor" : "none"} stroke="currentColor" strokeWidth={2} viewBox="0 0 24 24">
                    <path strokeLinecap="round" strokeLinejoin="round" d="M5 5a2 2 0 012-2h10a2 2 0 012 2v16l-7-3.5L5 21V5z" />
                  </svg>
                </button>
              )}
              {canDiverge && (
                <button
                  type="button"
                  onClick={handleDivergeOpen}
                  title="Diverge — branch a new conversation from here"
                  className="px-3 py-2 text-accent hover:accent-tint-10 transition-colors"
                >
                  <svg className="w-4 h-4" fill="none" stroke="currentColor" strokeWidth={2} viewBox="0 0 24 24">
                    <path strokeLinecap="round" strokeLinejoin="round" d="M21 17h-8l-3.5-5H3" />
                    <path strokeLinecap="round" strokeLinejoin="round" d="M21 7h-8l-3.5 5" />
                    <path strokeLinecap="round" strokeLinejoin="round" d="M18 10l3-3-3-3" />
                    <path strokeLinecap="round" strokeLinejoin="round" d="M18 20l3-3-3-3" />
                  </svg>
                </button>
              )}
              {canEdit && (
                <button
                  type="button"
                  onClick={handleEdit}
                  title="Modify"
                  className="px-3 py-2 text-heading hover:bg-input transition-colors"
                >
                  <svg className="w-4 h-4" fill="none" stroke="currentColor" strokeWidth={2} viewBox="0 0 24 24">
                    <path strokeLinecap="round" strokeLinejoin="round" d="M11 5H6a2 2 0 00-2 2v11a2 2 0 002 2h11a2 2 0 002-2v-5m-1.414-9.414a2 2 0 112.828 2.828L11.828 15H9v-2.828l8.586-8.586z" />
                  </svg>
                </button>
              )}
              {canDelete && (
                <button
                  type="button"
                  onClick={handleCancel}
                  title="Delete"
                  className="px-3 py-2 text-red-400 hover:bg-red-600/10 transition-colors"
                >
                  <svg className="w-4 h-4" fill="none" stroke="currentColor" strokeWidth={2} viewBox="0 0 24 24">
                    <path strokeLinecap="round" strokeLinejoin="round" d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16" />
                  </svg>
                </button>
              )}
              <button
                type="button"
                onClick={() => setShowActions(false)}
                title="Close"
                className="px-2 py-2 text-dim hover:bg-input transition-colors"
              >
                <svg className="w-4 h-4" fill="none" stroke="currentColor" strokeWidth={2} viewBox="0 0 24 24">
                  <path strokeLinecap="round" strokeLinejoin="round" d="M6 18L18 6M6 6l12 12" />
                </svg>
              </button>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

// --- Tool log entry rendering helpers ---

const _tlIcon = () => "▸";
const _tlColor = () => "text-accent";

function ToolLogEntry({ entry }) {
  return (
    <div className="flex items-center gap-1.5 text-dim">
      <span className="shrink-0">{_tlIcon(entry)}</span>
      <span className={_tlColor(entry)}>{entry.name}</span>
      {entry.summary && <span className="text-faint truncate max-w-[160px]">{entry.summary}</span>}
    </div>
  );
}

/**
 * Standalone mini-bubble for tool entries parsed from message content.
 * Rendered as a separate element between ChatBubble components in the message list.
 */
function ToolLogBubble({ entries }) {
  const [expanded, setExpanded] = useState(false);
  if (!entries?.length) return null;

  const LIMIT = 5;
  const shouldCollapse = entries.length > LIMIT;

  return (
    <div className="flex justify-start my-2">
      <div className="max-w-[min(85%,30rem)] rounded-2xl px-4 py-2.5 bg-surface shadow-card rounded-bl-md">
        <div className="space-y-0.5 text-xs font-mono">
          {shouldCollapse && !expanded ? (
            <button type="button" onClick={() => setExpanded(true)} className="flex items-center gap-1.5 text-dim hover:text-accent">
              <span className="shrink-0">▸</span>
              <span>{entries.length} tool calls</span>
            </button>
          ) : (
            <>
              {entries.map((e, j) => <ToolLogEntry key={j} entry={e} />)}
              {shouldCollapse && (
                <button type="button" onClick={() => setExpanded(false)} className="text-faint hover:text-dim text-[11px] mt-0.5">
                  collapse
                </button>
              )}
            </>
          )}
        </div>
      </div>
    </div>
  );
}


// --- Tool Activity Log (real-time feed of tool calls via CC hooks) ---

function ActiveToolEntry({ entry, nameColor }) {
  const [elapsed, setElapsed] = useState(() => Math.floor((serverNow() - entry.startTime) / 1000));
  useEffect(() => {
    const timer = setInterval(() => {
      setElapsed(Math.floor((serverNow() - entry.startTime) / 1000));
    }, 1000);
    return () => clearInterval(timer);
  }, [entry.startTime]);

  return (
    <div className="flex items-center gap-1.5 text-dim">
      {entry.kind === "permission" ? (
        <span className="text-amber-400 shrink-0">&#x23F3;</span>
      ) : (
        <span className="inline-block w-1.5 h-1.5 rounded-full bg-accent animate-pulse shrink-0" />
      )}
      <span className={nameColor(entry)}>{entry.name}</span>
      {entry.summary && <span className="text-faint truncate max-w-[160px]">{entry.summary}</span>}
      {entry.kind === "permission" && <span className="text-amber-400/70">awaiting permission</span>}
      {entry.kind !== "permission" && elapsed > 2 && <span className="text-faint">({elapsed}s)</span>}
    </div>
  );
}

function ToolActivityBubble({ message }) {
  const meta = message.metadata || {};
  const toolName = meta.tool_name || "";
  const toolKind = meta.tool_kind || "tool";
  const phase = meta.phase || "";
  const outputSummary = meta.output_summary || "";
  const isError = meta.is_error || false;
  const isDone = phase === "end" || message.status === "COMPLETED";
  const summary = message.content || "";

  const icon = () => {
    if (!isDone) return <span className="animate-pulse">&#9679;</span>;
    if (isError) return "\u2717";
    if (toolKind === "subagent") return "\u25C6";
    return "\u2713";
  };
  const nameColor = () => {
    if (toolKind === "permission") return "text-amber-400";
    if (toolKind === "subagent") return "text-violet-400";
    if (toolKind === "compact") return "text-orange-600 dark:text-orange-400";
    return "text-accent";
  };

  return (
    <div className="flex justify-start my-1">
      <div className="max-w-[min(85%,30rem)] rounded-2xl px-3 py-1.5 bg-surface shadow-card rounded-bl-md">
        <div className={`flex items-center gap-1.5 text-xs font-mono ${isError ? "text-red-400" : "text-dim"}`}>
          <span className="shrink-0">{icon()}</span>
          <span className={nameColor()}>{toolName}</span>
          {summary && <span className="text-faint truncate max-w-[160px]">{summary}</span>}
          {outputSummary && (
            <span className={isError ? "text-red-400/70" : "text-faint"}>&rarr; {outputSummary}</span>
          )}
        </div>
      </div>
    </div>
  );
}




// --- Typing Indicator (shown when executing but no streaming content yet) ---

function TypingIndicator() {
  return (
    <div className="flex justify-start my-2">
      <div className="bg-surface shadow-card rounded-2xl rounded-bl-md px-5 py-3.5">
        <div className="flex items-center gap-[5px]">
          <span className="typing-dot" style={{ animationDelay: "0ms" }} />
          <span className="typing-dot" style={{ animationDelay: "200ms" }} />
          <span className="typing-dot" style={{ animationDelay: "400ms" }} />
        </div>
      </div>
    </div>
  );
}


// --- Initializing Indicator (shown when agent is starting with no messages) ---

function InitializingIndicator() {
  return (
    <div className="flex flex-col items-center justify-center py-16 gap-4">
      <div className="flex items-center gap-[5px]">
        <span className="typing-dot" style={{ animationDelay: "0ms" }} />
        <span className="typing-dot" style={{ animationDelay: "200ms" }} />
        <span className="typing-dot" style={{ animationDelay: "400ms" }} />
      </div>
      <span className="text-sm text-dim">Starting agent...</span>
    </div>
  );
}

// --- Sync Prompt (shown when a CLI session is detected but not yet synced) ---

function SyncPrompt({ agentId, onSync }) {
  const [syncing, setSyncing] = useState(false);
  const handleSync = async () => {
    setSyncing(true);
    try {
      await wakeSync(agentId);
      // Give the sync loop a moment to import, then refresh
      setTimeout(() => onSync(), SYNC_SETTLE_DELAY);
    } catch {
      setSyncing(false);
    }
  };
  return (
    <div className="flex flex-col items-center justify-center py-16 gap-4">
      <span className="text-sm text-dim">CLI session detected</span>
      <button
        onClick={handleSync}
        disabled={syncing}
        className="px-4 py-2 rounded-lg bg-bubble text-bubble-ink text-sm font-medium hover:opacity-90 disabled:opacity-50 transition-colors"
      >
        {syncing ? "Syncing..." : "Sync Now"}
      </button>
    </div>
  );
}

// --- Streaming Bubble (live output while agent is executing) ---

// --- Send Later Time Picker ---

import SendLaterPicker from "../components/SendLaterPicker";
import SkillPickerPanel from "../components/SkillPickerPanel";

// --- Chat Input ---

function ChatInput({ agentId, project, onSend, onSendLater, disabled, disabledReason, isBusy, tmuxMode, onEscape, escapeUrgent, escapeAvailable = true, escapeDisabled = false, scrollButton, kbOpen = false, editingMessage, onEditSave, onEditCancel }) {
  const [text, setText] = useDraft(agentId ? `chat:${agentId}` : null, "");
  // Edit mode: a queued/scheduled message's content is loaded here instead
  // of the bubble (the composer sits above the mobile keyboard, the bubble
  // doesn't). The localStorage draft in `text` is untouched — it comes back
  // when the edit is saved or cancelled.
  const isEditing = !!editingMessage;
  const [editText, setEditText] = useState("");
  const [showPicker, setShowPicker] = useState(false);
  const [skillsDismissed, setSkillsDismissed] = useState(false);
  const [escCooldown, setEscCooldown] = useState(false);
  const inputBarRef = useRef(null);
  const skillPickerRef = useRef(null);

  // Slash-trigger: text is `/` followed by 0+ non-space chars (no spaces yet)
  const slashMatch = isEditing ? null : (text || "").match(/^\/(\S*)$/);
  const skillQuery = slashMatch ? slashMatch[1] : "";
  const showSkills = !!slashMatch && !skillsDismissed;

  // Reset dismissal when user backspaces / clears the slash trigger
  useEffect(() => {
    if (!slashMatch) setSkillsDismissed(false);
  }, [slashMatch]);

  const handleSkillSelect = (name) => {
    setText(`/${name} `);
    setSkillsDismissed(false);
    textareaRef.current?.focus();
  };

  const [attPreviewIndex, setAttPreviewIndex] = useState(null);
  const attachmentCacheKey = agentId ? `draft:chat:${agentId}:attachments` : null;
  const [attachments, setAttachments] = useState(() => {
    if (!attachmentCacheKey) return [];
    try {
      const cached = localStorage.getItem(attachmentCacheKey);
      if (cached) {
        return JSON.parse(cached).map((a) => ({
          ...a,
          uploading: false,
          file: null,
          previewUrl: a.thumbnailUrl || null,
        }));
      }
    } catch { /* ignore */ }
    return [];
  });
  const [uploadError, setUploadError] = useState(null);
  const [dragOver, setDragOver] = useState(false);
  const dragCountRef = useRef(0);
  const textareaRef = useRef(null);
  const fileInputRef = useRef(null);
  const pendingSendRef = useRef(null);

  // Voice transcript delivery: append directly to the chat's draft localStorage
  // key. localStorage is the single source of truth for chat drafts; the key
  // (`draft:chat:<agentId>`) is structurally bound to the chat ID, so cross-
  // chat misroute is physically impossible — no ref-snapshotting, no guards.
  // useDraft listens for BroadcastChannel + storage events and re-reads the
  // value into React state, so the textarea updates immediately whether or not
  // the chat is currently mounted on screen.
  //
  // The hook's atomic `claimVoiceJob` already ensures pipeline+recovery cannot
  // both deliver the same recording. The hook's persistKey-match gate still
  // prevents claiming when the user is in a different chat (so the IDB job is
  // preserved for recovery on return). Beyond those, no caller-side guards.
  const voice = useVoiceRecorder({
    onTranscript: (t) => {
      const draftKey = agentId ? `draft:chat:${agentId}` : null;
      if (draftKey) appendVoiceDraft(draftKey, t);
    },
    onError: (msg) => setVoiceError(msg),
    persistKey: agentId ? `voice:chat:${agentId}` : null,
  });
  const [voiceError, setVoiceError] = useState(null);
  useEffect(() => {
    if (voiceError) {
      const t = setTimeout(() => setVoiceError(null), ERROR_TOAST_DURATION);
      return () => clearTimeout(t);
    }
  }, [voiceError]);

  useEffect(() => {
    if (uploadError) {
      const t = setTimeout(() => setUploadError(null), ERROR_TOAST_DURATION);
      return () => clearTimeout(t);
    }
  }, [uploadError]);

  // Auto-grow textarea
  useEffect(() => {
    const el = textareaRef.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = Math.min(el.scrollHeight, 160) + "px";
  }, [text, editText]);

  // Entering edit mode (or switching to another message's edit): seed the
  // textarea with the message content and focus it, caret at end.
  useEffect(() => {
    if (!editingMessage) return;
    setEditText(editingMessage.content || "");
    const ta = textareaRef.current;
    if (ta) {
      ta.focus();
      requestAnimationFrame(() => {
        try { ta.setSelectionRange(ta.value.length, ta.value.length); } catch { /* ignore */ }
      });
    }
  }, [editingMessage?.id]); // eslint-disable-line react-hooks/exhaustive-deps

  // Cleanup blob URLs on unmount (only revoke actual blob: URLs, not server URLs)
  useEffect(() => {
    return () => {
      attachments.forEach((a) => { if (a.previewUrl?.startsWith("blob:")) URL.revokeObjectURL(a.previewUrl); });
    };
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  // Reload per-chat state when agentId changes. ChatInput is reused across
  // /agents/:id navigation (no key prop on the parent), so useState initializers
  // don't re-run. Without this, attachments and transient UI state from the
  // previous chat leak into the new one.
  const lastAttachmentKeyRef = useRef(attachmentCacheKey);
  useEffect(() => {
    if (lastAttachmentKeyRef.current === attachmentCacheKey) return;
    lastAttachmentKeyRef.current = attachmentCacheKey;
    // Revoke any blob: URLs from outgoing chat before swapping
    setAttachments((prev) => {
      prev.forEach((a) => { if (a.previewUrl?.startsWith("blob:")) URL.revokeObjectURL(a.previewUrl); });
      if (!attachmentCacheKey) return [];
      try {
        const cached = localStorage.getItem(attachmentCacheKey);
        if (cached) {
          return JSON.parse(cached).map((a) => ({
            ...a,
            uploading: false,
            file: null,
            previewUrl: a.thumbnailUrl || null,
          }));
        }
      } catch { /* ignore */ }
      return [];
    });
    // Reset transient per-chat UI state
    setAttPreviewIndex(null);
    setShowPicker(false);
    setSkillsDismissed(false);
    setEscCooldown(false);
    setDragOver(false);
    dragCountRef.current = 0;
    pendingSendRef.current = null;
  }, [attachmentCacheKey]);

  // Sync completed attachments to localStorage cache.
  // Guard: skip until the reload effect above has caught up to the new key.
  // Without this, on agentId change React fires both effects in the same
  // commit; this one would read the previous chat's `attachments` state
  // (setAttachments hasn't flushed yet) and write it under the NEW key.
  useEffect(() => {
    if (!attachmentCacheKey) return;
    if (lastAttachmentKeyRef.current !== attachmentCacheKey) return;
    const completed = attachments.filter((a) => !a.uploading && a.uploadedPath);
    if (completed.length > 0) {
      const toCache = completed.map((a) => ({
        id: a.id,
        uploadedPath: a.uploadedPath,
        originalName: a.originalName,
        size: a.size,
        mimeType: a.mimeType || a.file?.type || null,
        thumbnailUrl: a.thumbnailUrl || (
          (a.mimeType || a.file?.type || "").startsWith("image/")
            ? uploadUrl(a.uploadedPath.split("/").pop())
            : null
        ),
      }));
      try { safeSetItem(attachmentCacheKey, JSON.stringify(toCache)); } catch { /* ignore */ }
    } else {
      try { localStorage.removeItem(attachmentCacheKey); } catch { /* ignore */ }
    }
  }, [attachments, attachmentCacheKey]);

  const buildMessageText = useCallback((baseText, atts) => {
    let msg = baseText;
    for (const a of atts) {
      if (a.uploadedPath) msg += `\n[Attached file: ${a.uploadedPath}]`;
    }
    return msg;
  }, []);

  const clearAttachments = useCallback(() => {
    setAttachments((prev) => {
      prev.forEach((a) => { if (a.previewUrl?.startsWith("blob:")) URL.revokeObjectURL(a.previewUrl); });
      return [];
    });
    if (attachmentCacheKey) {
      try { localStorage.removeItem(attachmentCacheKey); } catch { /* ignore */ }
    }
  }, [attachmentCacheKey]);

  const handleSend = useCallback(async () => {
    const uploading = attachments.some((a) => a.uploading);
    if (uploading) {
      pendingSendRef.current = "send";
      return;
    }
    if (attachments.some((a) => a.error)) return;
    const uploaded = attachments.filter((a) => a.uploadedPath);
    if (!text.trim() && uploaded.length === 0) return;
    if (disabled && !isBusy) return;
    const msg = buildMessageText(text.trim(), uploaded);
    setText("");
    clearAttachments();
    pendingSendRef.current = null;
    await onSend(msg);
  }, [text, attachments, disabled, isBusy, onSend, setText, buildMessageText, clearAttachments]);

  const handleSchedule = useCallback(async (scheduledAt) => {
    const uploading = attachments.some((a) => a.uploading);
    if (uploading) {
      pendingSendRef.current = { type: "schedule", scheduledAt };
      return;
    }
    if (attachments.some((a) => a.error)) return;
    const uploaded = attachments.filter((a) => a.uploadedPath);
    if (!text.trim() && uploaded.length === 0) return;
    const msg = buildMessageText(text.trim(), uploaded);
    const savedText = text;
    setText("");
    clearAttachments();
    setShowPicker(false);
    pendingSendRef.current = null;
    try {
      await onSendLater(msg, scheduledAt);
    } catch (_) {
      if (!textareaRef.current?.value) setText(savedText);
    }
  }, [text, attachments, onSendLater, setText, buildMessageText, clearAttachments]);

  // Commit an edit — parent updates the queued/scheduled message and
  // clears editingMessage, which drops the composer back to the draft.
  const handleEditSaveClick = useCallback(() => {
    const trimmed = editText.trim();
    if (!trimmed) return;
    onEditSave?.(trimmed);
  }, [editText, onEditSave]);

  // Refs so the upload-complete effect always calls the latest version
  // without depending on handleSend/handleSchedule (which change on every
  // keystroke because they capture `text`).
  const handleSendRef = useRef(handleSend);
  handleSendRef.current = handleSend;
  const handleScheduleRef = useRef(handleSchedule);
  handleScheduleRef.current = handleSchedule;

  // Fire deferred send ONLY when attachments change (upload completes/fails).
  // Don't auto-send if any attachment failed — let the user retry or remove it.
  useEffect(() => {
    if (!pendingSendRef.current) return;
    if (attachments.some((a) => a.uploading)) return;
    if (attachments.some((a) => a.error)) return;
    const pending = pendingSendRef.current;
    pendingSendRef.current = null;
    if (pending === "send") {
      handleSendRef.current();
    } else if (pending?.type === "schedule") {
      handleScheduleRef.current(pending.scheduledAt);
    }
  }, [attachments]);

  const addFiles = useCallback((files) => {
    for (const file of files) {
      const id = Math.random().toString(36).slice(2, 10);
      const isImage = file.type.startsWith("image/");
      const previewUrl = isImage ? URL.createObjectURL(file) : null;

      setAttachments((prev) => [...prev, {
        id, file, previewUrl, uploading: true, uploadedPath: null,
        originalName: file.name, size: file.size, mimeType: file.type,
      }]);

      uploadFile(file, project).then((result) => {
        setAttachments((prev) => prev.map((a) =>
          a.id === id ? { ...a, uploading: false, uploadedPath: result.path } : a
        ));
      }).catch((err) => {
        setAttachments((prev) => prev.map((a) =>
          a.id === id ? { ...a, uploading: false, error: err.message } : a
        ));
      });
    }
  }, [project]);

  const retryUpload = useCallback((attId) => {
    setAttachments((prev) => {
      const att = prev.find((a) => a.id === attId);
      if (!att?.file) return prev;
      return prev.map((a) => a.id === attId ? { ...a, uploading: true, error: null } : a);
    });
    // Find the attachment's file from current state
    const att = attachments.find((a) => a.id === attId);
    if (!att?.file) return;
    uploadFile(att.file, project).then((result) => {
      setAttachments((prev) => prev.map((a) =>
        a.id === attId ? { ...a, uploading: false, uploadedPath: result.path, error: null } : a
      ));
    }).catch((err) => {
      setAttachments((prev) => prev.map((a) =>
        a.id === attId ? { ...a, uploading: false, error: err.message } : a
      ));
    });
  }, [attachments]);

  const handleFileSelect = useCallback((e) => {
    const files = Array.from(e.target.files || []);
    e.target.value = "";
    if (files.length > 0) addFiles(files);
  }, [addFiles]);

  const handleDragEnter = useCallback((e) => {
    e.preventDefault();
    e.stopPropagation();
    if (isEditing) return; // file intake belongs to the draft, not an edit
    dragCountRef.current++;
    if (e.dataTransfer?.types?.includes("Files")) setDragOver(true);
  }, [isEditing]);

  const handleDragLeave = useCallback((e) => {
    e.preventDefault();
    e.stopPropagation();
    dragCountRef.current--;
    if (dragCountRef.current <= 0) { dragCountRef.current = 0; setDragOver(false); }
  }, []);

  const handleDragOver = useCallback((e) => {
    e.preventDefault();
    e.stopPropagation();
  }, []);

  const handleDrop = useCallback((e) => {
    e.preventDefault();
    e.stopPropagation();
    dragCountRef.current = 0;
    setDragOver(false);
    if (isEditing) return;
    const files = Array.from(e.dataTransfer?.files || []);
    if (files.length > 0) addFiles(files);
  }, [addFiles, isEditing]);

  const handlePaste = useCallback((e) => {
    if (isEditing) return; // plain-text paste still works natively
    const items = e.clipboardData?.items;
    if (!items) return;
    const files = [];
    for (const item of items) {
      if (item.kind === "file") {
        const file = item.getAsFile();
        if (file) files.push(file);
      }
    }
    if (files.length > 0) {
      e.preventDefault();
      addFiles(files);
    }
  }, [addFiles, isEditing]);

  const removeAttachment = useCallback((id) => {
    setAttachments((prev) => {
      const att = prev.find((a) => a.id === id);
      if (att?.previewUrl?.startsWith("blob:")) URL.revokeObjectURL(att.previewUrl);
      return prev.filter((a) => a.id !== id);
    });
  }, []);

  const handleKeyDown = (e) => {
    if (isEditing) {
      if (e.key === "Escape") { e.preventDefault(); onEditCancel?.(); return; }
      if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); handleEditSaveClick(); }
      return;
    }
    if (showSkills && skillPickerRef.current?.hasItems()) {
      if (e.key === "ArrowDown") { e.preventDefault(); skillPickerRef.current.next(); return; }
      if (e.key === "ArrowUp") { e.preventDefault(); skillPickerRef.current.prev(); return; }
      if (e.key === "Enter" || e.key === "Tab") { e.preventDefault(); skillPickerRef.current.commit(); return; }
      if (e.key === "Escape") { e.preventDefault(); setSkillsDismissed(true); return; }
    }
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      handleSend();
    }
  };

  const handleEscape = async () => {
    if (!onEscape || escCooldown) return;
    setEscCooldown(true);
    try {
      await onEscape();
    } finally {
      // Cooldown on the frontend to match backend rate limit
      setTimeout(() => setEscCooldown(false), ESCAPE_COOLDOWN);
    }
  };

  // Editing bypasses the disabled gate — updating a pre-sent message hits
  // the DB, not the agent, so it works even when the composer is disabled.
  const canType = !disabled || isBusy || isEditing;
  const anyUploading = attachments.some((a) => a.uploading);
  const anyFailed = attachments.some((a) => a.error);
  const hasContent = text.trim() || attachments.some((a) => a.uploadedPath);
  const sendDisabled = (disabled && !isBusy) || !hasContent || anyUploading || anyFailed;
  const saveDisabled = !editText.trim();

  // No-op: keyboard dismiss handled by App-level focusout micro-scroll
  const handleBlur = useCallback(() => {}, []);

  return (
    <div
      className={`absolute left-0 right-0 flex flex-col items-center px-4 z-20 pointer-events-none ${kbOpen ? "" : "pb-[13px]"}`}
      style={{ bottom: 'var(--kb-h, 0px)' }}
    >
      {scrollButton}
      <div
        ref={inputBarRef}
        className={`glass-bar-nav rounded-[22px] px-3 pt-2 ${kbOpen ? "pb-1" : "pb-2.5"} flex flex-col gap-2 w-full relative pointer-events-auto`}
        style={{ maxWidth: "24rem" }}
        onDragEnter={handleDragEnter}
        onDragLeave={handleDragLeave}
        onDragOver={handleDragOver}
        onDrop={handleDrop}
      >
        {/* Drop zone overlay */}
        {dragOver && (
          <div className="absolute inset-0 z-30 rounded-[22px] accent-tint-15 border-2 border-dashed border-accent flex items-center justify-center pointer-events-none">
            <span className="text-sm font-medium text-accent">Drop files here</span>
          </div>
        )}
        {/* Edit-mode banner: pencil + original snippet + cancel ✕ */}
        {isEditing && (
          <div className="flex items-center gap-2 px-2 pt-1">
            <svg className="w-4 h-4 text-accent shrink-0" fill="none" stroke="currentColor" strokeWidth={2} viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" d="M11 5H6a2 2 0 00-2 2v11a2 2 0 002 2h11a2 2 0 002-2v-5m-1.414-9.414a2 2 0 112.828 2.828L11.828 15H9v-2.828l8.586-8.586z" />
            </svg>
            <div className="flex-1 min-w-0 border-l-2 accent-edge-60 pl-2">
              <p className="text-[11px] font-medium text-accent leading-tight">Editing message</p>
              <p className="text-xs text-dim truncate leading-tight">{editingMessage.content}</p>
            </div>
            <button
              type="button"
              onClick={onEditCancel}
              title="Cancel edit (Esc)"
              className="shrink-0 w-7 h-7 rounded-full flex items-center justify-center text-dim hover:text-heading hover:bg-input transition-colors"
            >
              <svg className="w-4 h-4" fill="none" stroke="currentColor" strokeWidth={2} viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" d="M6 18L18 6M6 6l12 12" />
              </svg>
            </button>
          </div>
        )}
        <textarea
          ref={textareaRef}
          value={isEditing ? editText : text}
          onChange={(e) => (isEditing ? setEditText(e.target.value) : setText(e.target.value))}
          onKeyDown={handleKeyDown}
          onPaste={handlePaste}
          onBlur={handleBlur}
          placeholder={isEditing ? "Edit message..." : tmuxMode ? "Send via tmux..." : isBusy ? "Send (queued until ready)..." : disabled ? disabledReason : "Type a message..."}
          disabled={!canType}
          rows={2}
          className="w-full min-h-[48px] max-h-[180px] rounded-xl bg-transparent px-3 py-2 text-sm text-heading placeholder-hint resize-none focus:outline-none transition-colors disabled:opacity-50"
        />
        {(voice.streamingText || voice.refining) && (
          <div className="px-3 pb-1 text-sm text-accent-70 italic animate-pulse">
            {voice.refining ? "Refining..." : voice.streamingText}
          </div>
        )}
        {/* Attachment preview chips (hidden while editing — they belong to the draft) */}
        {!isEditing && attachments.length > 0 && (
          <div className="flex flex-wrap gap-1.5 px-1">
            {attachments.map((att, i) => (
              <div key={att.id} className={`flex items-center gap-1 px-2 py-1 rounded-lg text-xs max-w-[160px] cursor-pointer ${att.error ? "danger-tint-15 ring-1 ring-danger-30" : "bg-elevated"}`}
                onClick={() => { if (!att.uploading && !att.error) setAttPreviewIndex(i); }}>
                {att.previewUrl ? (
                  <img src={att.previewUrl} alt="" className={`w-8 h-8 rounded object-cover shrink-0 ${att.error ? "opacity-50" : ""}`} />
                ) : (
                  <svg className="w-4 h-4 text-dim shrink-0" fill="none" stroke="currentColor" strokeWidth={1.5} viewBox="0 0 24 24">
                    <path strokeLinecap="round" strokeLinejoin="round" d="M19.5 14.25v-2.625a3.375 3.375 0 00-3.375-3.375h-1.5A1.125 1.125 0 0113.5 7.125v-1.5a3.375 3.375 0 00-3.375-3.375H8.25m2.25 0H5.625c-.621 0-1.125.504-1.125 1.125v17.25c0 .621.504 1.125 1.125 1.125h12.75c.621 0 1.125-.504 1.125-1.125V11.25a9 9 0 00-9-9z" />
                  </svg>
                )}
                <span className={`truncate flex-1 min-w-0 ${att.error ? "text-red-400" : "text-label"}`}>
                  {att.error ? "Failed" : att.originalName}
                </span>
                {att.uploading ? (
                  <svg className="w-3.5 h-3.5 text-accent animate-spin shrink-0" fill="none" viewBox="0 0 24 24">
                    <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
                    <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z" />
                  </svg>
                ) : att.error ? (
                  <div className="flex items-center gap-0.5 shrink-0">
                    <button type="button" onClick={(e) => { e.stopPropagation(); retryUpload(att.id); }} className="text-accent hover:opacity-80" title="Retry upload">
                      <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" strokeWidth={2} viewBox="0 0 24 24">
                        <path strokeLinecap="round" strokeLinejoin="round" d="M16.023 9.348h4.992v-.001M2.985 19.644v-4.992m0 0h4.992m-4.993 0l3.181 3.183a8.25 8.25 0 0013.803-3.7M4.031 9.865a8.25 8.25 0 0113.803-3.7l3.181 3.182" />
                      </svg>
                    </button>
                    <button type="button" onClick={(e) => { e.stopPropagation(); removeAttachment(att.id); }} className="text-dim hover:text-heading" title="Remove">
                      <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" strokeWidth={2} viewBox="0 0 24 24">
                        <path strokeLinecap="round" strokeLinejoin="round" d="M6 18L18 6M6 6l12 12" />
                      </svg>
                    </button>
                  </div>
                ) : (
                  <button type="button" onClick={(e) => { e.stopPropagation(); removeAttachment(att.id); }} className="text-dim hover:text-heading shrink-0">
                    <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" strokeWidth={2} viewBox="0 0 24 24">
                      <path strokeLinecap="round" strokeLinejoin="round" d="M6 18L18 6M6 6l12 12" />
                    </svg>
                  </button>
                )}
              </div>
            ))}
          </div>
        )}
        {(uploadError || voiceError) && (
          <p className="text-xs text-red-400 px-1">{uploadError || voiceError}</p>
        )}
        <input ref={fileInputRef} type="file" accept="image/*,video/*,.pdf,.txt,.csv,.json,.md,.py,.js,.ts,.jsx,.tsx,.html,.css,.yaml,.yml,.xml,.log,.zip,.tar,.gz" multiple className="hidden" onChange={handleFileSelect} />
        <div className="flex items-center gap-1.5 px-1">
          {/* Attach button (hidden while editing — edits are text-only) */}
          {!isEditing && (
          <button
            type="button"
            onClick={() => fileInputRef.current?.click()}
            title="Attach files"
            className="shrink-0 w-10 h-10 rounded-full flex items-center justify-center transition-colors bg-elevated hover:bg-hover text-label"
          >
            <svg className="w-5 h-5" fill="none" stroke="currentColor" strokeWidth={2} viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" d="M12 4v16m8-8H4" />
            </svg>
          </button>
          )}
          <div className="flex-1 min-w-0 flex items-center justify-end gap-1.5">
            {!isEditing && (<>
            {voice.recording && voice.remainingSeconds != null && (
              <span className={`text-xs font-semibold tabular-nums ${voice.remainingSeconds <= 10 ? "text-red-400" : "text-red-500"}`}>
                {voice.remainingSeconds >= 60
                  ? `${Math.floor(voice.remainingSeconds / 60)}:${String(voice.remainingSeconds % 60).padStart(2, "0")}`
                  : voice.remainingSeconds}
              </span>
            )}
            <VoiceRecorder
              recording={voice.recording}
              voiceLoading={voice.voiceLoading}
              refining={voice.refining}
              micError={voice.micError}
              onToggle={voice.toggleRecording}
            />
            </>)}
          </div>
          {/* Escape button — sends Esc to tmux (always visible for tmux agents, disabled when stopped/error) */}
          {!isEditing && onEscape && (
            <button
              type="button"
              onClick={handleEscape}
              disabled={escapeDisabled || !escapeAvailable || !escapeUrgent || escCooldown}
              title={escapeDisabled ? "Agent is stopped" : !escapeAvailable ? "No tmux pane attached" : "Send Escape to agent (dismiss prompt)"}
              className={`shrink-0 w-10 h-10 rounded-full flex items-center justify-center transition-colors ${
                escapeDisabled || !escapeAvailable
                  ? "bg-elevated text-dim/30 cursor-not-allowed"
                  : escapeUrgent && !escCooldown
                    ? "bg-red-500/80 hover:bg-red-500 text-white cursor-pointer"
                    : "bg-elevated text-dim cursor-not-allowed"
              }`}
            >
              <svg className="w-5 h-5" fill="none" stroke="currentColor" strokeWidth={2.5} viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" d="M6 18L18 6M6 6l12 12" />
              </svg>
            </button>
          )}
          {/* Send later (clock) button */}
          {!isEditing && (
          <div className="relative">
            <button
              type="button"
              onClick={() => hasContent && setShowPicker(!showPicker)}
              disabled={!hasContent}
              title="Schedule message for later"
              className={`shrink-0 w-10 h-10 rounded-full flex items-center justify-center transition-colors ${
                !hasContent
                  ? "bg-elevated text-dim cursor-not-allowed"
                  : "bg-amber-500 hover:bg-amber-400 text-white"
              }`}
            >
              <svg className="w-5 h-5" fill="none" stroke="currentColor" strokeWidth={2} viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" d="M12 6v6l4 2m6-2a10 10 0 11-20 0 10 10 0 0120 0z" />
              </svg>
            </button>
            {showPicker && (
              <SendLaterPicker
                title="Schedule At"
                onSelect={handleSchedule}
                onClose={() => setShowPicker(false)}
              />
            )}
          </div>
          )}
          {/* Send button — becomes a save (✓) button in edit mode */}
          {isEditing ? (
            <button
              type="button"
              onClick={handleEditSaveClick}
              disabled={saveDisabled}
              title="Save edit"
              className={`shrink-0 w-10 h-10 rounded-full flex items-center justify-center transition-colors ${
                saveDisabled
                  ? "bg-elevated text-dim cursor-not-allowed"
                  : "bg-accent hover:opacity-90 text-accent-ink"
              }`}
            >
              <svg className="w-5 h-5" fill="none" stroke="currentColor" strokeWidth={2.5} viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" d="M5 13l4 4L19 7" />
              </svg>
            </button>
          ) : (
          <button
            type="button"
            onClick={handleSend}
            disabled={sendDisabled}
            className={`shrink-0 w-10 h-10 rounded-full flex items-center justify-center transition-colors ${
              sendDisabled
                ? "bg-elevated text-dim cursor-not-allowed"
                : "bg-accent hover:opacity-90 text-accent-ink"
            }`}
          >
            <svg className="w-5 h-5" fill="none" stroke="currentColor" strokeWidth={2} viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" d="M6 12L3.269 3.126A59.768 59.768 0 0121.485 12 59.77 59.77 0 013.27 20.876L5.999 12zm0 0h7.5" />
            </svg>
          </button>
          )}
        </div>
      </div>
      {showSkills && (
        <SkillPickerPanel
          ref={skillPickerRef}
          project={project}
          query={skillQuery}
          anchorEl={inputBarRef.current}
          onSelect={handleSkillSelect}
          onClose={() => setSkillsDismissed(true)}
        />
      )}
      {attPreviewIndex != null && attachments.length > 0 && (
        <ImageLightbox
          media={attachments.filter(a => !a.uploading).map(a => ({
            src: a.previewUrl || uploadUrl(a.uploadedPath?.split("/").pop()),
            filename: a.originalName,
            type: "image",
          }))}
          initialIndex={Math.min(attPreviewIndex, attachments.filter(a => !a.uploading).length - 1)}
          onClose={() => setAttPreviewIndex(null)}
        />
      )}
    </div>
  );
}

// --- Main Page ---

export default function AgentChatPage({ theme, onToggleTheme, agentId: propAgentId, embedded, onClose, onNavigateAgent }) {
  const { id: routeId } = useParams();
  const id = propAgentId || routeId;
  const navigate = useNavigate();
  const location = useLocation();
  const visible = usePageVisible();
  // Seed from the brief cache (populated by AgentsPage / ProjectDetailPage)
  // so the chat header — agent name, project, status pill — paints
  // immediately without waiting for fetchAgent. Full agent data still
  // arrives via loadData() and overwrites this seed.
  const [agent, setAgent] = useState(() => agentBriefCache.get(id) || null);
  const [taskData, setTaskData] = useState(null);
  // Split source-of-truth: sentMessages mirrors /display/sent (file-backed,
  // append-only, byte-incremental); preSentMessages mirrors /display/pre-sent
  // (in-memory snapshot, full-replace). Render derives a unified `messages`
  // via useMemo for legacy filter compatibility, but mutations target the
  // correct source state directly.
  const [sentMessages, setSentMessages] = useState([]);
  const [preSentMessages, setPreSentMessages] = useState([]);
  const [failedMessages, setFailedMessages] = useState([]);
  const messages = useMemo(
    () => [...sentMessages, ...preSentMessages],
    [sentMessages, preSentMessages],
  );
  // Composer-based edit of a queued/scheduled message: {id, content} while
  // active. The bubble gets a highlight ring and ChatInput swaps into edit
  // mode (the old inline-bubble textarea was covered by the mobile keyboard).
  const [editingMessage, setEditingMessage] = useState(null);
  // Auto-exit edit mode when the message leaves the editable pre-sent zone
  // (stop hook dispatched it, it was cancelled elsewhere, or chat switched).
  useEffect(() => {
    if (!editingMessage) return;
    const stillEditable = preSentMessages.some(
      (m) => m.id === editingMessage.id && (m.status === "queued" || m.status === "scheduled"),
    );
    if (!stillEditable) setEditingMessage(null);
  }, [preSentMessages, editingMessage]);
  const [hasMore, setHasMore] = useState(false);
  const [loadingMore, setLoadingMore] = useState(false);
  // Focus-slice mode: when initial load was a focus_id slice, the right
  // edge of the read window may be < EOF. hasLater reflects "more bytes
  // past the slice"; loadNewerMessages closes the gap on scroll-down.
  const [hasLater, setHasLater] = useState(false);
  const [loadingLater, setLoadingLater] = useState(false);
  const [loading, setLoading] = useState(true);
  const toastCtx = useToast();
  // Only one chat-bubble action menu open at a time across the whole list.
  const [openMenuMsgId, setOpenMenuMsgId] = useState(null);
  // Set of message ids that already have a bookmark on the server. Refreshed
  // when the agent / project is known. Used to seed the initial filled state
  // of the bookmark icons in the action menu and on attachment ActionButtons.
  const [bookmarkedSet, setBookmarkedSet] = useState(() => new Set());
  const refreshBookmarkedSet = useCallback(() => {
    const proj = agent?.project;
    if (!proj) return () => {};
    let cancelled = false;
    fetchProjectBookmarks(proj)
      .then((rows) => {
        if (cancelled || !Array.isArray(rows)) return;
        setBookmarkedSet(new Set(rows.map((r) => r.message_id)));
      })
      .catch(() => { /* non-fatal */ });
    return () => { cancelled = true; };
  }, [agent?.project]);
  useEffect(() => refreshBookmarkedSet(), [refreshBookmarkedSet]);
  // Cross-device invalidation: another device added/removed/edited a
  // bookmark in this project → backend emits project_update → refetch so
  // the inline bookmark icon on each chat bubble reflects truth.
  useWsEvent(useCallback((event) => {
    if (event.type !== "project_update") return;
    clog(`[chat] project_update WS event=${event.data?.name} my-project=${agent?.project} match=${event.data?.name === agent?.project}`);
    if (event.data?.name !== agent?.project) return;
    refreshBookmarkedSet();
  }, [agent?.project, refreshBookmarkedSet]));
  // After a successful bookmark POST, prompt for an optional user note.
  const [noteForBookmarkId, setNoteForBookmarkId] = useState(null);
  const handleAfterBookmark = useCallback((messageId, action) => {
    if (action === "removed") {
      setBookmarkedSet((prev) => {
        if (!prev.has(messageId)) return prev;
        const next = new Set(prev); next.delete(messageId); return next;
      });
      setNoteForBookmarkId((cur) => (cur === messageId ? null : cur));
      return;
    }
    setBookmarkedSet((prev) => {
      if (prev.has(messageId)) return prev;
      const next = new Set(prev); next.add(messageId); return next;
    });
    setNoteForBookmarkId(messageId);
  }, []);
  const [showStopConfirm, setShowStopConfirm] = useState(false);
  const [stopping, setStopping] = useState(false);
  const [generateSummary, setGenerateSummary] = useState(false);
  const [taskOutcome, setTaskOutcome] = useState("complete"); // "complete" | "incomplete" | "drop"
  const [incompleteReason, setIncompleteReason] = useState("");
  const [feedbackAttachments, setFeedbackAttachments] = useState([]);
  const feedbackFileInputRef = useRef(null);
  const feedbackVoice = useVoiceRecorder({
    // Drop the delivery if the user navigated to a different chat between
    // recording-start and pipeline-finish. Without this guard, the latent
    // setIncompleteReason setter would write to AgentChatPage's shared state
    // under the wrong chat's modal context. Recovery is also blocked below.
    onTranscript: (t, recordingKey) => {
      const expected = id ? `voice:feedback:${id}` : null;
      if (recordingKey !== expected) return;
      setIncompleteReason((prev) => (prev ? prev + " " + t : t));
    },
    onError: (msg) => console.warn("Feedback voice error:", msg),
    persistKey: id ? `voice:feedback:${id}` : null,
  });
  // Navigating away from a chat while feedback voice is in flight: stop the
  // recorder for the OUTGOING chat and wipe its IDB job so neither pipeline
  // nor recovery can later inject into the current chat's modal. Together
  // with the onTranscript guard above, this enforces "navigate out → drop
  // the voice entirely" semantics for stop-modal feedback.
  const feedbackVoiceRef = useRef(feedbackVoice);
  feedbackVoiceRef.current = feedbackVoice;
  useEffect(() => {
    return () => {
      if (feedbackVoiceRef.current?.recording) feedbackVoiceRef.current.stopRecording();
      if (id) deleteVoiceJob(`voice:feedback:${id}`).catch(() => {});
    };
  }, [id]);
  const [showTaskCard, setShowTaskCard] = useState(false);
  const [selectedAttempt, setSelectedAttempt] = useState(null);
  const [showScrollToBottom, setShowScrollToBottom] = useState(false);
  // Sync generateSummary with per-project localStorage preference
  const generateSummaryInitialized = useRef(false);
  useEffect(() => {
    if (!agent?.project || generateSummaryInitialized.current) return;
    const stored = localStorage.getItem(`pref:generateSummary:${agent.project}`);
    if (stored !== null) setGenerateSummary(stored === "true");
    generateSummaryInitialized.current = true;
  }, [agent?.project]);
  useEffect(() => {
    if (!agent?.project || !generateSummaryInitialized.current) return;
    localStorage.setItem(`pref:generateSummary:${agent.project}`, String(generateSummary));
  }, [generateSummary, agent?.project]);

  // Feedback attachment helpers
  const addFeedbackFiles = (files) => {
    for (const file of files) {
      const fid = Math.random().toString(36).slice(2, 10);
      const isImage = file.type.startsWith("image/");
      const previewUrl = isImage ? URL.createObjectURL(file) : null;
      setFeedbackAttachments((prev) => [...prev, { id: fid, file, previewUrl, uploading: true, uploadedPath: null, originalName: file.name, size: file.size, mimeType: file.type }]);
      uploadFile(file, agent?.project).then((result) => {
        setFeedbackAttachments((prev) => prev.map((a) => a.id === fid ? { ...a, uploading: false, uploadedPath: result.path } : a));
      }).catch(() => {
        setFeedbackAttachments((prev) => prev.filter((a) => a.id !== fid));
        if (previewUrl) URL.revokeObjectURL(previewUrl);
      });
    }
  };
  const removeFeedbackAttachment = (fid) => {
    setFeedbackAttachments((prev) => {
      const att = prev.find((a) => a.id === fid);
      if (att?.previewUrl?.startsWith("blob:")) URL.revokeObjectURL(att.previewUrl);
      return prev.filter((a) => a.id !== fid);
    });
  };
  const clearFeedbackAttachments = () => {
    setFeedbackAttachments((prev) => { prev.forEach((a) => { if (a.previewUrl?.startsWith("blob:")) URL.revokeObjectURL(a.previewUrl); }); return []; });
  };
  const handleFeedbackPaste = (e) => {
    const items = e.clipboardData?.items;
    if (!items) return;
    const files = [];
    for (const item of items) { if (item.kind === "file") { const f = item.getAsFile(); if (f) files.push(f); } }
    if (files.length > 0) { e.preventDefault(); addFeedbackFiles(files); }
  };

  const [resuming, setResuming] = useState(false);
  const [starred, setStarred] = useState(false);
  const [starLoading, setStarLoading] = useState(false);
  const [muted, setMuted] = useState(() => isAgentMuted(id));
  const [deferredTo, setDeferredTo] = useState(null);
  const [showDeferPicker, setShowDeferPicker] = useState(false);
  // Refs, not state: nothing renders these — they are only consulted inside
  // the WS handler — and as state every tool_activity event (several per
  // second) re-rendered the whole page.
  const activeToolRef = useRef(null);
  const toolStartTimeRef = useRef(null);
  // Permission cards are now persisted in DB as interactive messages (no WS-only state needed)
  const generationIdRef = useRef(null); // tracks current backend generation_id
  // Debug: ring buffer of recent WS events for frontend-state reporter
  const wsEventLog = useRef([]);
  const pushWsEvent = (type, data) => {
    const entry = `${new Date().toISOString().slice(11,23)} ${type} ${JSON.stringify(data).slice(0,200)}`;
    wsEventLog.current.push(entry);
    if (wsEventLog.current.length > 50) wsEventLog.current.shift();
  };
  const [editingName, setEditingName] = useState(false);
  const [nameDraft, setNameDraft] = useState("");
  const nameInputRef = useRef(null);
  const [fileModal, setFileModal] = useState(null); // "CLAUDE.md" | "PROGRESS.md" | null
  const [showBrowser, setShowBrowser] = useState(false);
  const [showTerminal, setShowTerminal] = useState(false);
  const [fileExists, setFileExists] = useState({ "CLAUDE.md": null, "PROGRESS.md": null });
  const [headerExpanded, setHeaderExpanded] = useState(false);
  const [showIdPopover, setShowIdPopover] = useState(false);
  const [idPopoverPos, setIdPopoverPos] = useState(null);
  const idPillRef = useRef(null);
  const idPopoverRef = useRef(null);
  const idPressTimerRef = useRef(null);
  const idLongPressFiredRef = useRef(false);
  const idClickTimerRef = useRef(null);
  const idHoverCloseTimerRef = useRef(null);
  const idCancelHoverClose = useCallback(() => {
    if (idHoverCloseTimerRef.current) {
      clearTimeout(idHoverCloseTimerRef.current);
      idHoverCloseTimerRef.current = null;
    }
  }, []);
  const idScheduleHoverClose = useCallback(() => {
    idCancelHoverClose();
    idHoverCloseTimerRef.current = setTimeout(() => {
      idHoverCloseTimerRef.current = null;
      setShowIdPopover(false);
    }, 150);
  }, [idCancelHoverClose]);
  // Close on outside pointerdown — replaces the full-screen overlay that was
  // intercepting hover and click events on the pill itself.
  useEffect(() => {
    if (!showIdPopover) return;
    const handleDocDown = (e) => {
      if (idPillRef.current?.contains(e.target)) return;
      if (idPopoverRef.current?.contains(e.target)) return;
      setShowIdPopover(false);
    };
    document.addEventListener("pointerdown", handleDocDown);
    return () => document.removeEventListener("pointerdown", handleDocDown);
  }, [showIdPopover]);
  const [syncRefreshing, setSyncRefreshing] = useState(false);
  const messagesEndRef = useRef(null);
  const einkModeRef = useRef(false);
  const health = useHealthStatus();
  const { sysStats } = useMonitor();
  // Gate non-critical fetches (context-usage, suggestions) until after the
  // initial display load completes — keeps them off the critical path so
  // they don't compete with /display/sent for backend workers.
  const [criticalLoadDone, setCriticalLoadDone] = useState(false);
  // Reads from agent.context_* (persisted on the row); no separate fetch.
  // Header pill renders immediately from briefCache; WS pushes live updates.
  const contextUsage = useContextUsage(agent);

  const showToast = useCallback((message, type = "success") => {
    if (type === "error") toastCtx.error(message);
    else toastCtx.success(message);
  }, [toastCtx]);

  // Load agent + messages with AbortController support.
  // On initial load, errors propagate to console so failures are visible.
  // On subsequent poll refreshes, errors are silenced (transient network issues).
  const initialLoadDone = useRef(false);
  const abortRef = useRef(null);
  const messagesRef = useRef([]);
  const hasPendingInteractiveRef = useRef(false);
  // Display API cursor tracking
  const nextOffsetRef = useRef(0);
  const hasEarlierRef = useRef(false);
  const hasLaterRef = useRef(false);

  // Keep messagesRef in sync.
  useEffect(() => {
    messagesRef.current = messages;
  }, [messages]);

  // Apply /display/sent response to sentMessages.
  // initial=true replaces all (preserving WS-delivered_at on collisions);
  // initial=false merges incrementally (handles _replace entries from update_last).
  // Also evicts any matching ids from preSentMessages (defends against the
  // pre_sent_tombstoned signal arriving after message_sent on promote).
  const applySentData = useCallback((data, { initial = false } = {}) => {
    if (data.next_offset != null) {
      nextOffsetRef.current = data.next_offset;
    }
    if (data.has_earlier != null) {
      hasEarlierRef.current = !!data.has_earlier;
    }
    if (data.has_later != null) {
      hasLaterRef.current = !!data.has_later;
    }

    let newIds = null;
    setSentMessages((prev) => {
      // Incremental poll with nothing appended: keep the same array so the
      // whole message list is not reconciled every 3 s for no change.
      if (!initial && !(data.messages || []).length) return prev;
      const byId = new Map(initial ? [] : prev.map((m) => [m.id, m]));
      const prevById = (initial && prev.length)
        ? new Map(prev.map((m) => [m.id, m]))
        : null;

      for (const msg of data.messages || []) {
        if (prevById) {
          const p = prevById.get(msg.id);
          if (p?.delivered_at && (!msg.delivered_at || p.delivered_at > msg.delivered_at)) {
            byId.set(msg.id, { ...msg, delivered_at: p.delivered_at });
            continue;
          }
        }
        byId.set(msg.id, msg);
      }
      newIds = new Set((data.messages || []).map((m) => m.id));
      return [...byId.values()];
    });

    // Evict promoted ids from preSentMessages so we don't double-render.
    if (newIds && newIds.size) {
      setPreSentMessages((prev) => prev.filter((e) => !newIds.has(e.id)));
    }
  }, []);

  // Apply /display/pre-sent snapshot to preSentMessages — full replace.
  const applyPreSentData = useCallback((snapshot) => {
    const entries = snapshot.entries || [];
    setPreSentMessages((prev) => (
      prev.length === entries.length
        && prev.every((e, i) => e === entries[i] || JSON.stringify(e) === JSON.stringify(entries[i]))
        ? prev
        : entries
    ));
  }, []);

  // Initial load: fetch agent + sent (tail) + pre-sent (snapshot) in parallel.
  const loadData = useCallback(async () => {
    abortRef.current?.abort();
    const controller = new AbortController();
    abortRef.current = controller;
    try {
      nextOffsetRef.current = 0;

      // Focus-id from URL: on first load, ask the backend for a slice
      // centered on this message instead of the file tail. Falls back
      // gracefully to a tail load if the id can't be located.
      const focusId = new URLSearchParams(location.search).get("focus");
      const initialParams = focusId
        ? { tailBytes: 50000, focusId }
        : { tailBytes: 50000 };

      // Critical-path fetches: only display data is needed for first-bubble.
      // fetchAgent is moved off the critical path — the agent list endpoint
      // already populated briefCache with everything we need (task_id, muted,
      // has_pending_suggestions, etc.); the only chat-page-specific extra
      // field is `successor_id`, which is fine to land a frame later.
      const cachedBrief = agentBriefCache.get(id);
      // Hover-prefetched? Only valid for the no-focus path (prefetch uses
      // default tail params; with ?focus= we need a centered slice).
      const prefetched = !focusId ? consumePrefetch(id) : null;
      const [sentData, preSentData] = await (prefetched || Promise.all([
        fetchDisplaySent(id, initialParams),
        fetchDisplayPreSent(id),
      ]));
      if (controller.signal.aborted) return;

      // Apply display data immediately — this is what first-bubble needs.
      applySentData(sentData, { initial: true });
      applyPreSentData(preSentData);
      setHasMore(!!sentData.has_earlier);
      setHasLater(!!sentData.has_later);
      // Unlock auto-scroll / focus-restore / saved-position effects as soon
      // as messages are renderable. Previously setLoading(false) only fired
      // in the finally block (after background fetchAgent), so scrollTop=0
      // stayed visible for ~500ms before scroll snapped to the right place.
      // fetchAgent is background-only now and shouldn't gate this.
      setLoading(false);

      // Use briefCache as the agent stand-in if available, so the conditional
      // UI gates (status pill, ProgressSuggestionsCard, InsightsHistoryCard)
      // can render correctly without waiting on fetchAgent.
      // First mount only: seed agent state from cachedBrief (if available)
      // so conditional UI gates can paint without waiting on fetchAgent.
      // Subsequent loadData() calls (stop/resume/retry/etc) MUST NOT touch
      // `agent`; the cache is up to ~5s stale relative to WS-pushed state,
      // and clobbering would briefly revert insight_status / has_pending /
      // status to old values (root cause of the apply-bubble disappearing
      // and the Generating/Ready bubble jumping).
      if (cachedBrief && !initialLoadDone.current) {
        setAgent(cachedBrief);
        // briefCache already has task_id — fetch the task in parallel with
        // the background fetchAgent so the Task chip lands at the same time
        // as messages, not a frame later.
        if (cachedBrief.task_id && !taskData) {
          fetchTaskV2(cachedBrief.task_id).then(t => setTaskData(t)).catch(() => {});
        }
      }
      setCriticalLoadDone(true);

      // Restore active tool state — check the last tool_activity in the
      // combined window for an unfinished one.
      {
        const allMsgs = [...(sentData.messages || []), ...(preSentData.entries || [])];
        const lastToolMsg = [...allMsgs].reverse().find((m) => m.kind === "tool_activity");
        if (lastToolMsg && lastToolMsg.status !== "COMPLETED") {
          const meta = lastToolMsg.metadata || {};
          activeToolRef.current = ({ name: meta.tool_name || "", summary: lastToolMsg.content || "" });
          toolStartTimeRef.current = (new Date(lastToolMsg.created_at).getTime());
        }
      }

      // Background-refresh the agent record. If briefCache was cold, this is
      // also our authoritative load and gates the not-found early-return.
      // Wrap separately so a transient fetchAgent failure doesn't show a
      // "Failed to load" toast when display data already rendered fine.
      let agentData = null;
      try {
        agentData = await fetchAgent(id, { includeSubagents: false });
      } catch (err) {
        if (controller.signal.aborted) return;
        if (!cachedBrief) throw err; // cold cache → propagate to outer catch
        console.error("AgentChatPage: background fetchAgent failed (using cached brief)", err);
      }
      if (controller.signal.aborted) return;
      if (!agentData || !agentData.id) {
        // Only error out if we had nothing to render with in the first place.
        if (!cachedBrief) return;
      } else if (!initialLoadDone.current) {
        // Authoritative agent fetch only on first mount. After that, agent
        // state is maintained exclusively by WS (agent_update / *_ready
        // events); a wholesale setAgent here would race with WS patches and
        // re-introduce the bubble flicker even with the cachedBrief gate.
        setAgent(agentData);
        if (
          agentData.task_id &&
          !taskData &&
          agentData.task_id !== cachedBrief?.task_id
        ) {
          fetchTaskV2(agentData.task_id).then(t => setTaskData(t)).catch(() => {});
        }
        if (agentData.muted != null) {
          setMuted(agentData.muted);
          setAgentMuted(id, agentData.muted);
        }
        if (agentData.deferred_to !== undefined) {
          setDeferredTo(agentData.deferred_to || null);
        }
      }
      initialLoadDone.current = true;
    } catch (err) {
      if (controller.signal.aborted) return;
      if (!initialLoadDone.current) {
        console.error("AgentChatPage: initial load failed", err);
        showToast("Failed to load agent: " + (err.message || "Unknown error"), "error");
      }
    } finally {
      if (!controller.signal.aborted) setLoading(false);
    }
  }, [id, showToast, location.search]);

  // Load older sent messages (scroll-up pagination). Pre-sent state is
  // unaffected — it's a fixed-size snapshot, not paginated.
  const loadOlderMessages = useCallback(async () => {
    const current = messagesRef.current;
    if (!current.length || loadingMore) return;
    setLoadingMore(true);
    try {
      let older = [];
      let moreFlag = false;
      if (hasEarlierRef.current) {
        const data = await fetchDisplaySent(id, { offset: 0, tailBytes: nextOffsetRef.current });
        older = Array.isArray(data?.messages) ? data.messages : [];
        moreFlag = !!data?.has_earlier;
        hasEarlierRef.current = moreFlag;
      }
      if (older.length) {
        const el = scrollContainerRef.current;
        if (el) savedScrollHeight.current = el.scrollHeight;
        setSentMessages((prev) => {
          const seenIds = new Set(prev.map((m) => m.id));
          const unique = older.filter((m) => !seenIds.has(m.id));
          return unique.length ? [...unique, ...prev] : prev;
        });
      }
      setHasMore(moreFlag);
    } catch (err) {
      console.warn("Failed to load older messages:", err);
    } finally {
      setLoadingMore(false);
    }
  }, [id, loadingMore]);

  // Load newer sent messages (scroll-down pagination). Only meaningful
  // after a focus-slice initial load; in tail mode hasLaterRef is always
  // false and this is a no-op. Suppresses the auto-bottom-scroll path
  // by marking userScrolledUp, since we're appending below the
  // already-anchored focus point.
  const loadNewerMessages = useCallback(async () => {
    if (loadingLater || !hasLaterRef.current) return;
    setLoadingLater(true);
    try {
      const data = await fetchDisplaySent(id, { offset: nextOffsetRef.current });
      const newer = Array.isArray(data?.messages) ? data.messages : [];
      hasLaterRef.current = !!data?.has_later;
      // Advance the read cursor so subsequent incremental polls
      // (debouncedRefreshSent / refreshMessages, no longer gated once
      // hasLater flips false) start from EOF instead of re-fetching the
      // slice→EOF range we just appended.
      if (data?.next_offset != null) {
        nextOffsetRef.current = data.next_offset;
      }
      if (newer.length) {
        userScrolledUp.current = true;
        setSentMessages((prev) => {
          const seenIds = new Set(prev.map((m) => m.id));
          const unique = newer.filter((m) => !seenIds.has(m.id));
          return unique.length ? [...prev, ...unique] : prev;
        });
      }
      setHasLater(hasLaterRef.current);
    } catch (err) {
      console.warn("Failed to load newer messages:", err);
    } finally {
      setLoadingLater(false);
    }
  }, [id, loadingLater]);

  // "Go to live tail" — used by the scroll-to-bottom button. In tail
  // mode this is just a scroll. In focus-slice mode the loaded window
  // stops short of EOF, so we extend it first (loadNewerMessages reads
  // slice-end → EOF in one shot). The actual scrollIntoView is deferred
  // to a useLayoutEffect that fires AFTER React commits the appended
  // messages — awaiting `requestAnimationFrame` directly was unreliable
  // under React 18 concurrent rendering: rAF could fire before commit,
  // landing scrollIntoView on the OLD messagesEndRef position.
  const pendingTailScrollRef = useRef(false);
  const scrollToLiveTail = useCallback(async () => {
    if (hasLaterRef.current) {
      pendingTailScrollRef.current = true;
      await loadNewerMessages();
      // Don't scroll here — let the layoutEffect run after commit.
      return;
    }
    messagesEndRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [loadNewerMessages]);

  // Incremental refresh: refetch sent (byte-incremental) and pre-sent
  // (full snapshot). Both are cheap; refreshing both keeps any cross-bucket
  // promote transition consistent regardless of WS event arrival order.
  // In focus-slice mode (hasLaterRef=true) we skip the sent fetch so a
  // single WS event doesn't dump everything between the slice end and EOF
  // into one render — the user advances the cursor via loadNewerMessages.
  const refreshMessages = useCallback(async () => {
    try {
      const agentData = await fetchAgent(id, { includeSubagents: false });
      if (!agentData || !agentData.id) return;
      setAgent((prev) => (sameAgentRecord(prev, agentData) ? prev : agentData));

      const isInitial = nextOffsetRef.current === 0;
      const skipSent = hasLaterRef.current;
      const sentParams = isInitial
        ? { tailBytes: 50000 }
        : { offset: nextOffsetRef.current };

      const [sentData, preSentData] = await Promise.all([
        skipSent ? Promise.resolve(null) : fetchDisplaySent(id, sentParams),
        fetchDisplayPreSent(id),
      ]);
      if (sentData) applySentData(sentData, { initial: isInitial });
      applyPreSentData(preSentData);
    } catch {
      // Transient errors during polling — silently ignore
    }
  }, [id, applySentData, applyPreSentData]);

  // Initial load + clear notification flag for this agent
  useEffect(() => {
    clearAgentNotified(id);
    registerViewing(id);
    initialLoadDone.current = false;
    setLoading(true);
    loadData();
    return () => { abortRef.current?.abort(); unregisterViewing(id); };
  }, [loadData, id]);

  // Track per-pane user interaction for time-accounting. In split-screen
  // multiple AgentChatPage instances share one WS, so the backend can't
  // tell which pane the user is actually using — we update a per-agent
  // interaction timestamp here and the WS context picks the freshest one
  // as the "primary" view.
  useEffect(() => {
    const el = kbContainerRef.current;
    if (!el || !id) return;
    let lastCallAt = 0;
    const onAct = () => {
      const now = Date.now();
      if (now - lastCallAt < 1500) return;
      lastCallAt = now;
      touchAgentInteraction(id);
    };
    el.addEventListener("mousemove", onAct, { passive: true });
    el.addEventListener("keydown", onAct, { passive: true });
    el.addEventListener("wheel", onAct, { passive: true });
    el.addEventListener("touchstart", onAct, { passive: true });
    el.addEventListener("pointerdown", onAct, { passive: true });
    return () => {
      el.removeEventListener("mousemove", onAct);
      el.removeEventListener("keydown", onAct);
      el.removeEventListener("wheel", onAct);
      el.removeEventListener("touchstart", onAct);
      el.removeEventListener("pointerdown", onAct);
    };
  }, [id]);

  // Track e-ink mode state for gesture handlers
  useEffect(() => {
    einkModeRef.current = getEinkMode();
  }, []);

  // E-ink mode: double-finger horizontal swipe to page scroll (left=down, right=up)
  useEffect(() => {
    let touchState = null;

    const handleTouchStart = (e) => {
      if (!einkModeRef.current) return;
      if (!touchState) {
        // First finger of a new gesture — capture start point.
        touchState = {
          touchCount: e.touches.length,
          startX: e.touches[0].clientX,
          startY: e.touches[0].clientY,
        };
      } else {
        // Additional finger landed during ongoing gesture —
        // record the peak finger count, keep the original start point.
        touchState.touchCount = Math.max(touchState.touchCount, e.touches.length);
      }
    };

    const handleTouchEnd = (e) => {
      if (!einkModeRef.current || !touchState) return;
      // Wait until ALL fingers have lifted. touchend fires once per finger,
      // so an intermediate fire (length > 0) means another finger is still
      // down — don't drop touchState yet, or the multi-finger gesture
      // never gets to run when fingers lift one-at-a-time.
      if (e.touches.length > 0) return;

      const endX = e.changedTouches[0].clientX;
      const endY = e.changedTouches[0].clientY;
      const deltaX = endX - touchState.startX;
      const deltaY = endY - touchState.startY;
      const distance = Math.hypot(deltaX, deltaY);

      if (distance < 50) {
        touchState = null;
        return;
      }

      // Only double-finger horizontal swipes: left = scroll down, right = scroll up
      if (touchState.touchCount >= 2 && Math.abs(deltaX) > Math.abs(deltaY)) {
        e.preventDefault();
        const sc = scrollContainerRef.current;
        if (sc) {
          const vh = sc.clientHeight;
          const isLeftSwipe = deltaX < 0;
          const newScrollTop = isLeftSwipe
            ? Math.min(sc.scrollTop + vh, sc.scrollHeight - vh)
            : Math.max(sc.scrollTop - vh, 0);
          sc.scrollTop = newScrollTop;
        }
      }

      touchState = null;
    };

    document.addEventListener("touchstart", handleTouchStart, { passive: true });
    document.addEventListener("touchend", handleTouchEnd, { passive: false });
    return () => {
      document.removeEventListener("touchstart", handleTouchStart);
      document.removeEventListener("touchend", handleTouchEnd);
    };
  }, []);

  // Check if any interactive cards are waiting for an answer
  // (must be before polling useEffect which depends on it)
  const hasPendingInteractive = useMemo(() => {
    // Only block input when agent is actively waiting (IDLE) for a response
    if (agent?.status !== "IDLE") return false;

    // Only check the LAST agent message with interactive metadata
    // (old cards are stale — agent has continued past them)
    let lastInteractiveMsg = null;
    for (let i = messages.length - 1; i >= 0; i--) {
      const msg = messages[i];
      if (msg.role === "AGENT" && msg.metadata?.interactive?.length) {
        lastInteractiveMsg = msg;
        break;
      }
    }
    if (!lastInteractiveMsg) return false;

    for (const item of lastInteractiveMsg.metadata.interactive) {
      // Skip auto-approved items
      if (item.auto_approved) continue;
      // Skip dismissed items
      const answer = item.answer;
      if (typeof answer === "string" && (
        answer.startsWith("The user doesn't want to proceed") ||
        answer.startsWith("User declined") ||
        answer.startsWith("Tool use rejected")
      )) continue;

      const questions = item.questions || [];
      if (questions.length > 1) {
        // Multi-question: pending if any question lacks a selection
        const indices = item.selected_indices || {};
        for (let qi = 0; qi < questions.length; qi++) {
          if (indices[String(qi)] == null && answer == null) return true;
        }
      } else {
        // Single-question or ExitPlanMode
        if (answer == null && item.selected_index == null) return true;
      }
    }
    return false;
  }, [messages, agent?.status]);
  hasPendingInteractiveRef.current = hasPendingInteractive;

  // Bottom-area user messages: active queue (queued / cancelled). Sourced
  // directly from preSentMessages — no need to filter the merged messages
  // array since preSent entries live in their own state now.
  const queuedMessages = useMemo(
    () => preSentMessages.filter((m) =>
      m.role === "USER" && !m.scheduled_at && (
        m.status === "queued" || m.status === "cancelled"
      ),
    ),
    [preSentMessages],
  );
  // Subset that still needs dispatch — drives escape-button urgency and
  // bulk cancel. Excludes cancelled entries (they are terminal pre-sent).
  const activeQueuedMessages = useMemo(
    () => queuedMessages.filter((m) => m.status !== "cancelled"),
    [queuedMessages],
  );

  // Polling — faster when executing, pauses when page hidden
  useEffect(() => {
    if (!visible) return;
    const isActive = agent?.status === "EXECUTING" || agent?.status === "IDLE" || hasPendingInteractiveRef.current;
    const interval = isActive ? POLL_ACTIVE_INTERVAL : POLL_IDLE_INTERVAL;
    const timer = setInterval(refreshMessages, interval);
    return () => clearInterval(timer);
  }, [refreshMessages, agent?.status, visible, hasPendingInteractive]);

  // Mark as read on mount and when new messages arrive
  useEffect(() => {
    if (agent && agent.unread_count > 0) {
      markAgentRead(id).then(() => {
        window.dispatchEvent(new CustomEvent("agents-data-changed"));
      }).catch((err) => {
        console.warn("Failed to mark agent as read:", err);
      });
    }
  }, [id, messages.length, agent?.unread_count]);

  // Fetch starred status once agent is loaded
  useEffect(() => {
    if (!agent) return;
    const sessionId = agent.session_id || agent.id;
    fetchProjectSessions(agent.project)
      .then((fetchedSessions) => {
        const match = fetchedSessions.find((s) => s.session_id === sessionId);
        setStarred(match?.starred ?? false);
      })
      .catch((err) => {
        console.warn("Failed to fetch starred status:", err);
      });
  }, [agent?.project, agent?.session_id, agent?.id]);

  // Check CLAUDE.md / PROGRESS.md existence once agent is loaded
  useEffect(() => {
    if (!agent?.project) return;
    Promise.all([
      fetchProjectFile(agent.project, "CLAUDE.md").catch(() => ({ exists: false })),
      fetchProjectFile(agent.project, "PROGRESS.md").catch(() => ({ exists: false })),
    ]).then(([c, p]) => {
      setFileExists({ "CLAUDE.md": c.exists, "PROGRESS.md": p.exists });
    });
  }, [agent?.project]);

  const handleToggleStar = async () => {
    if (!agent || starLoading) return;
    const sessionId = agent.session_id || agent.id;
    const prevStarred = starred;
    const nextStarred = !starred;
    // Optimistic: flip the star immediately so iPhone (high-latency self-signed
    // TLS over LAN) doesn't appear frozen for 1-3s waiting on the HTTP RTT.
    setStarred(nextStarred);
    window.dispatchEvent(new CustomEvent("agent-star-changed", { detail: { agentId: id, starred: nextStarred } }));
    setStarLoading(true);
    try {
      if (prevStarred) {
        await unstarSession(agent.project, sessionId);
      } else {
        await starSession(agent.project, sessionId);
      }
    } catch (err) {
      // Rollback on failure
      setStarred(prevStarred);
      window.dispatchEvent(new CustomEvent("agent-star-changed", { detail: { agentId: id, starred: prevStarred } }));
      showToast("Failed to update star: " + err.message, "error");
    } finally {
      setStarLoading(false);
    }
  };

  const handleToggleMute = () => {
    const nextMuted = !muted;
    setAgentMuted(id, nextMuted);
    setMuted(nextMuted);
    updateAgent(id, { muted: nextMuted }).catch((err) => {
      showToast("Failed to save mute setting: " + (err.message || "Unknown error"), "error");
    });
    showToast(nextMuted ? "Notifications muted for this agent" : "Notifications enabled for this agent");
  };

  const handleDeferSelect = async (iso) => {
    setShowDeferPicker(false);
    setDeferredTo(iso);
    try {
      await updateAgent(id, { deferred_to: iso });
      window.dispatchEvent(new CustomEvent("agent-defer-changed", { detail: { agentId: id, deferredTo: iso } }));
      const when = new Date(iso);
      showToast(`Notifications deferred until ${when.toLocaleString()}`);
    } catch (err) {
      setDeferredTo(null);
      showToast("Failed to defer agent: " + (err.message || "Unknown error"), "error");
    }
  };

  const handleDeferClear = async () => {
    setShowDeferPicker(false);
    const prev = deferredTo;
    setDeferredTo(null);
    try {
      await updateAgent(id, { deferred_to: null });
      window.dispatchEvent(new CustomEvent("agent-defer-changed", { detail: { agentId: id, deferredTo: null } }));
      showToast("Defer cleared");
    } catch (err) {
      setDeferredTo(prev);
      showToast("Failed to clear defer: " + (err.message || "Unknown error"), "error");
    }
  };

  // Sync mute & star state across split-screen panes
  useEffect(() => {
    const onMute = (e) => { if (e.detail?.agentId === id) setMuted(e.detail.muted); };
    const onStar = (e) => { if (e.detail?.agentId === id) setStarred(e.detail.starred); };
    const onDefer = (e) => { if (e.detail?.agentId === id) setDeferredTo(e.detail.deferredTo || null); };
    const onRename = (e) => { if (e.detail?.agentId === id) setAgent((prev) => prev ? { ...prev, name: e.detail.name } : prev); };
    window.addEventListener("agent-mute-changed", onMute);
    window.addEventListener("agent-star-changed", onStar);
    window.addEventListener("agent-defer-changed", onDefer);
    window.addEventListener("agent-renamed", onRename);
    return () => {
      window.removeEventListener("agent-mute-changed", onMute);
      window.removeEventListener("agent-star-changed", onStar);
      window.removeEventListener("agent-defer-changed", onDefer);
      window.removeEventListener("agent-renamed", onRename);
    };
  }, [id]);


  // Track keyboard height via visualViewport.  Uses direct DOM (CSS variable
  // --kb-h) for instant, jank-free updates.  Input bar uses transform (GPU-
  // composited, no layout reflow) instead of bottom.  Scroll container padding
  // is debounced to avoid expensive reflow every frame.
  const [kbOpen, setKbOpen] = useState(false);
  const kbContainerRef = useRef(null);
  useEffect(() => {
    const vv = window.visualViewport;
    if (!vv) return;
    let rafId = null;
    let stopTimer = null;
    let padTimer = null;
    let prevOff = 0;
    let isOpen = false;

    // Prevent touchmove outside the message scroll container so iOS
    // visual-viewport doesn't scroll when dragging on the input bar.
    const blockTouchOutsideScroll = (e) => {
      const sc = scrollContainerRef.current;
      if (sc && sc.contains(e.target)) return; // allow message list scroll
      // Don't interfere with overlay sheets (file browser, new task, etc.)
      if (e.target.closest('[data-overlay]')) return;
      e.preventDefault();
    };

    // --- KB debug logging: batch samples and flush to backend ---
    const kbSamples = [];
    let kbFlushTimer = null;
    const kbFlush = () => {
      if (!kbSamples.length) return;
      const batch = kbSamples.splice(0);
      fetch("/api/debug/kb-log", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ samples: batch }),
      }).catch(() => {});
    };
    const kbLog = (containerH, off, open) => {
      kbSamples.push({
        t: Date.now(),
        cH: containerH,
        iH: window.innerHeight,
        vvH: Math.round(vv.height),
        vvOT: Math.round(vv.offsetTop),
        off,
        open,
      });
      clearTimeout(kbFlushTimer);
      kbFlushTimer = setTimeout(kbFlush, 500);
    };

    const update = () => {
      const el = kbContainerRef.current;
      if (!el) return;

      const containerH = el.clientHeight;
      // rawDelta: detects keyboard presence (ignores viewport scroll)
      const rawDelta = Math.max(0, Math.round(containerH - vv.height));
      // kbOffset: actual positioning offset — subtracts vv.offsetTop so
      // the input bar stays flush with the keyboard even when iOS scrolls
      // the visual viewport (common on 2nd+ keyboard open).
      const kbOffset = Math.max(0, Math.round(containerH - vv.height - vv.offsetTop));

      kbLog(containerH, kbOffset, rawDelta > 100);

      const open = rawDelta > 100;

      if (open) {
        // Only update CSS var when change > 3px to suppress sub-pixel jitter
        if (Math.abs(kbOffset - prevOff) > 3) {
          prevOff = kbOffset;
          el.style.setProperty('--kb-h', `${kbOffset}px`);
          // Keep messages pinned to bottom while keyboard is animating
          // (margin-bottom shrinks the scroll container via --kb-h)
          const sc = scrollContainerRef.current;
          if (sc && !userScrolledUp.current) {
            sc.scrollTop = sc.scrollHeight;
          }
        }
      } else if (prevOff !== 0) {
        prevOff = 0;
        el.style.removeProperty('--kb-h');
      }

      // Scroll container padding: keep small so messages peek behind glass
      // input bar (like iMessage).  When KB is closed, pb-36 from the class
      // provides generous spacing instead.
      if (open !== isOpen) {
        clearTimeout(padTimer);
        padTimer = setTimeout(() => {
          const sc = scrollContainerRef.current;
          if (!sc) return;
          sc.style.paddingBottom = open ? 'calc(var(--kb-h, 0px) + 80px)' : '';
        }, 80);
      }

      if (open && !isOpen) {
        isOpen = true;
        // Lock body to prevent iOS viewport scroll — CSS lock is
        // applied once (no per-frame fight), so no shake.
        document.body.style.position = 'fixed';
        document.body.style.width = '100%';
        document.body.style.top = '0';
        document.body.style.touchAction = 'none';
        window.scrollTo(0, 0);
        // Block touchmove outside scroll container to prevent iOS
        // visual-viewport scroll via touch gestures on the input bar.
        document.addEventListener('touchmove', blockTouchOutsideScroll, { passive: false });
        setKbOpen(true);
        const sc = scrollContainerRef.current;
        if (sc && !userScrolledUp.current) {
          requestAnimationFrame(() => { sc.scrollTop = sc.scrollHeight; });
        }
      } else if (!open && isOpen) {
        isOpen = false;
        // Unlock body
        document.body.style.position = '';
        document.body.style.width = '';
        document.body.style.top = '';
        document.body.style.touchAction = '';
        document.removeEventListener('touchmove', blockTouchOutsideScroll);
        setKbOpen(false);
        const sc = scrollContainerRef.current;
        if (sc && !userScrolledUp.current) {
          requestAnimationFrame(() => { sc.scrollTop = sc.scrollHeight; });
        }
      }
    };

    const poll = () => {
      update();
      rafId = requestAnimationFrame(poll);
    };
    const startPoll = () => {
      if (stopTimer) { clearTimeout(stopTimer); stopTimer = null; }
      if (!rafId) rafId = requestAnimationFrame(poll);
    };
    const stopPoll = () => {
      // Delay stop — keyboard switch may briefly lose focus
      if (stopTimer) clearTimeout(stopTimer);
      stopTimer = setTimeout(() => {
        if (rafId) { cancelAnimationFrame(rafId); rafId = null; }
        update();
      }, 400);
    };
    vv.addEventListener("resize", update);
    vv.addEventListener("scroll", update);
    document.addEventListener("focusin", startPoll);
    document.addEventListener("focusout", stopPoll);
    return () => {
      vv.removeEventListener("resize", update);
      vv.removeEventListener("scroll", update);
      document.removeEventListener("focusin", startPoll);
      document.removeEventListener("focusout", stopPoll);
      if (rafId) cancelAnimationFrame(rafId);
      if (stopTimer) clearTimeout(stopTimer);
      clearTimeout(padTimer);
      document.removeEventListener('touchmove', blockTouchOutsideScroll);
      document.body.style.touchAction = '';
      kbFlush(); // flush remaining samples
    };
  }, []);

  // Auto-scroll to bottom on new messages or streaming content
  const scrollContainerRef = useRef(null);
  const userScrolledUp = useRef(false);
  const scrollSaveTimer = useRef(null);
  const prevLastMsgId = useRef(null);
  const prevFirstMsgId = useRef(null);
  const prevLastContentLen = useRef(0);
  const savedScrollHeight = useRef(null);
  const scrollKey = `scroll:chat:${id}`;
  const scrollCountKey = `scroll:chat:${id}:count`;

  // userScrolledUp = "user is NOT at the live tail" — true while they're
  // reading older content, so auto-pin-to-bottom skips. Two ways to be
  // off-tail: (a) physically scrolled up >100px from the bottom of the
  // loaded window, or (b) the loaded window doesn't reach EOF (focus-slice
  // mode), in which case even sitting at the slice's bottom edge isn't
  // "the tail". Without (b), focus-slice mode mistook a short slice for
  // tail and re-pinned on every WS event.
  const handleScroll = useCallback(() => {
    const el = scrollContainerRef.current;
    if (!el) return;
    const distFromBottom = el.scrollHeight - el.scrollTop - el.clientHeight;
    userScrolledUp.current = distFromBottom > 100 || hasLaterRef.current;
    const shouldShow = distFromBottom > 300;
    setShowScrollToBottom(shouldShow);
    // Scroll-up trigger for lazy loading
    if (el.scrollTop < 200 && hasMore && !loadingMore) {
      loadOlderMessages();
    }
    // Scroll-down trigger — only fires in focus-slice mode (hasLater).
    if (distFromBottom < 200 && hasLater && !loadingLater) {
      loadNewerMessages();
    }
    clearTimeout(scrollSaveTimer.current);
    scrollSaveTimer.current = setTimeout(() => {
      try { localStorage.setItem(scrollKey, String(el.scrollTop)); } catch { /* ignore */ }
    }, SCROLL_SAVE_DEBOUNCE);
  }, [scrollKey, hasMore, loadingMore, loadOlderMessages, hasLater, loadingLater, loadNewerMessages]);

  // Save scroll position on unmount
  useEffect(() => {
    return () => {
      clearTimeout(scrollSaveTimer.current);
      const el = scrollContainerRef.current;
      if (el) {
        try { localStorage.setItem(scrollKey, String(el.scrollTop)); } catch { /* ignore */ }
      }
    };
  }, [scrollKey]);

  // useLayoutEffect so scroll adjustments happen before browser paint (no flicker)
  useLayoutEffect(() => {
    if (loading || !messages.length) return;
    const lastMsg = messages[messages.length - 1];
    const lastId = lastMsg?.id;
    const firstId = messages[0]?.id;
    const lastContentLen = (lastMsg?.content || "").length;
    const isFirstLoad = prevLastMsgId.current === null;
    const newMessagesAppended = !isFirstLoad && prevLastMsgId.current !== lastId;
    const olderMessagesPrepended = !isFirstLoad && prevFirstMsgId.current !== firstId && prevLastMsgId.current === lastId;
    // Streaming: same lastId, but the last message's content grew (a new
    // chunk landed). Distinct from the "snapshot replaced with same data"
    // case where length is unchanged.
    const lastContentGrew = !isFirstLoad && prevLastMsgId.current === lastId && lastContentLen > prevLastContentLen.current;

    prevLastMsgId.current = lastId;
    prevFirstMsgId.current = firstId;
    prevLastContentLen.current = lastContentLen;

    if (isFirstLoad) {
      // Focus-slice mode: don't restore saved position or scroll to
      // bottom — the focus effect (below) will scrollIntoView the
      // bookmarked message and own the initial position.
      const hasFocus = !!new URLSearchParams(location.search).get("focus");
      if (hasFocus) {
        userScrolledUp.current = true;
        return;
      }
      // Restore saved position if message count matches (no new messages since last visit)
      try {
        const savedCount = localStorage.getItem(scrollCountKey);
        const savedPos = localStorage.getItem(scrollKey);
        if (savedPos && savedCount && Number(savedCount) === messages.length) {
          const el = scrollContainerRef.current;
          if (el) {
            el.scrollTop = Number(savedPos);
            userScrolledUp.current = true;
            return;
          }
        }
      } catch { /* ignore */ }
      // No saved position or count mismatch — scroll to bottom
      messagesEndRef.current?.scrollIntoView({ behavior: "instant" });
      return;
    }

    // Older messages prepended — preserve scroll position
    if (olderMessagesPrepended) {
      const el = scrollContainerRef.current;
      if (el && savedScrollHeight.current != null) {
        el.scrollTop += el.scrollHeight - savedScrollHeight.current;
      }
      return;
    }

    // New messages appended — clear saved position
    if (newMessagesAppended) {
      try {
        localStorage.removeItem(scrollKey);
        localStorage.setItem(scrollCountKey, String(messages.length));
      } catch { /* ignore */ }
    }
    // Auto-pin to bottom only when bottom-edge content actually changed:
    // a new message arrived (newMessagesAppended) or the last message is
    // streaming (lastContentGrew). Plain re-renders that swap messages
    // identity without changing bottom content (e.g. WS-driven pre-sent
    // snapshot replace with identical entries) are no-ops.
    if ((newMessagesAppended || lastContentGrew) && !userScrolledUp.current) {
      messagesEndRef.current?.scrollIntoView({ behavior: "smooth" });
    }
  }, [loading, messages, scrollKey, scrollCountKey]);

  // Keep saved message count in sync for future visits
  useEffect(() => {
    if (!loading && messages.length > 0) {
      try { localStorage.setItem(scrollCountKey, String(messages.length)); } catch { /* ignore */ }
    }
  }, [loading, messages.length, scrollCountKey]);

  // Focus + breathing-flash for bookmarked messages — `?focus=<message_id>` URL param.
  // If the bookmark targets a message older than the initial 50KB tail, keep
  // pulling older pages until it lands in the DOM (or there are no more).
  // useLayoutEffect (not useEffect) so the scroll lands BEFORE the browser
  // paints — otherwise the user sees a one-frame flash of "messages from
  // top" before the centered scroll, which reads as a flicker.
  const focusedMsgRef = useRef(false);
  useLayoutEffect(() => {
    if (focusedMsgRef.current) return;
    if (loading || !messages?.length) return;
    const params = new URLSearchParams(location.search);
    const focusId = params.get("focus");
    if (!focusId) return;
    const el = document.querySelector(`[data-msg-id="${CSS.escape(focusId)}"]`);
    if (!el) {
      if (hasMore && !loadingMore) loadOlderMessages();
      return;
    }
    // "instant" + setting userScrolledUp before the scroll lands is
    // critical: we start at scrollTop=0, and a smooth scroll would let
    // handleScroll fire mid-flight (scrollTop<200 trips loadOlderMessages,
    // which then prepends + adjusts scrollTop while the smooth scroll is
    // still in motion — landing the page in an inconsistent spot).
    userScrolledUp.current = true;
    el.scrollIntoView({ behavior: "instant", block: "center" });
    el.classList.add("bookmark-flash");
    setTimeout(() => el.classList.remove("bookmark-flash"), 2100);
    focusedMsgRef.current = true;
  }, [loading, messages?.length, location.search, hasMore, loadingMore, loadOlderMessages]);

  // Search-result text highlight — runs on every messages-identity change,
  // not gated by focusedMsgRef. On busy agents (streaming / heavy WS),
  // ChatBubble re-renders frequently and React's reconciliation strips the
  // <mark> nodes we injected (they're outside React's virtual DOM). Re-
  // apply each render so the highlight survives. highlightTextMatches is
  // idempotent — text inside an existing .search-hit is skipped. Cleared
  // naturally when the page unmounts.
  useLayoutEffect(() => {
    if (loading || !messages?.length) return;
    const params = new URLSearchParams(location.search);
    const focusId = params.get("focus");
    const q = (params.get("q") || "").trim();
    if (!focusId || !q) return;
    const el = document.querySelector(`[data-msg-id="${CSS.escape(focusId)}"]`);
    if (el) highlightTextMatches(el, q);
  }, [loading, messages, location.search]);

  // Flush a deferred "go to live tail" scroll. scrollToLiveTail sets the
  // pending flag and awaits loadNewerMessages; this layoutEffect runs
  // synchronously after React commits the appended messages (when the
  // `messages` dep updates), at which point messagesEndRef is at the
  // new bottom and scrollIntoView lands on EOF reliably — independent
  // of any rAF / scheduler timing.
  useLayoutEffect(() => {
    if (!pendingTailScrollRef.current) return;
    if (hasLaterRef.current) return;  // loadNewer hasn't completed
    pendingTailScrollRef.current = false;
    messagesEndRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages]);

  // Auto-load older messages when content doesn't overflow the viewport
  // but has_earlier is true (scroll-based trigger can't fire without overflow)
  useEffect(() => {
    if (loading || !messages.length || !hasMore || loadingMore) return;
    const el = scrollContainerRef.current;
    if (!el) return;
    // Content fits without scrollbar — onScroll will never fire
    if (el.scrollHeight <= el.clientHeight + 10) {
      loadOlderMessages();
    }
  }, [loading, messages, hasMore, loadingMore, loadOlderMessages]);

  // WebSocket: re-fetch on new_message events, handle streaming
  const { sendWsMessage } = useWebSocket();

  // Notify backend which agent we're viewing (suppresses notifications)
  useEffect(() => {
    sendWsMessage({ type: "viewing", agent_id: id });
    return () => sendWsMessage({ type: "viewing", agent_id: null, _unview: id });
  }, [id, sendWsMessage]);

  // Use subscriber-based WS handler — every event is delivered, none lost
  // to React 18 batching (unlike the old setLastEvent/useEffect pattern).
  const refreshMessagesRef = useRef(refreshMessages);
  refreshMessagesRef.current = refreshMessages;
  // Debounced refetches per channel. Bursts of same-channel signals
  // (e.g. delivered + executed in <50ms) collapse to one HTTP call.
  // pre_sent and sent are independently debounced — message_sent (which
  // crosses both) triggers both timers, which is correct.
  const refetchSentTimerRef = useRef(null);
  const refetchPreSentTimerRef = useRef(null);
  const debouncedRefreshSent = useCallback(() => {
    clearTimeout(refetchSentTimerRef.current);
    refetchSentTimerRef.current = setTimeout(async () => {
      // In focus-slice mode, skip sent polls until the user advances
      // past the slice via loadNewerMessages; otherwise a single WS
      // event would pull everything between slice end and EOF in one
      // render. Pre-sent state still flows through its own debouncer.
      if (hasLaterRef.current) return;
      try {
        const isInitial = nextOffsetRef.current === 0;
        const params = isInitial ? { tailBytes: 50000 } : { offset: nextOffsetRef.current };
        const data = await fetchDisplaySent(id, params);
        applySentData(data, { initial: isInitial });
      } catch {}
    }, 50);
  }, [id, applySentData]);
  const debouncedRefreshPreSent = useCallback(() => {
    clearTimeout(refetchPreSentTimerRef.current);
    refetchPreSentTimerRef.current = setTimeout(async () => {
      try {
        const data = await fetchDisplayPreSent(id);
        applyPreSentData(data);
      } catch {}
    }, 50);
  }, [id, applyPreSentData]);
  useEffect(() => () => {
    clearTimeout(refetchSentTimerRef.current);
    clearTimeout(refetchPreSentTimerRef.current);
  }, []);
  useWsEvent((event) => {
    // Project-scoped events have no agent_id; check before the agent_id guard.
    // Targeted star event — payload carries the new boolean, no refetch needed.
    if (event.type === "session_star_changed" && agent?.project && event.data?.project === agent.project) {
      const sessionId = agent.session_id || agent.id;
      if (event.data.session_id === sessionId) {
        setStarred(!!event.data.starred);
      }
      return;
    }

    if (event.data?.agent_id !== id) return;

    if (event.type === "agent_stream_end") {
      console.log('[ws] agent_stream_end', event.data);
      pushWsEvent('agent_stream_end', event.data);
      const gid = event.data.generation_id;
      if (gid != null && generationIdRef.current != null && gid < generationIdRef.current) return;
      activeToolRef.current = (null);
      toolStartTimeRef.current = (null);
      return;
    }

    if (event.type === "tool_activity") {
      console.log('[ws] tool_activity', event.data.tool_name, event.data.phase);
      pushWsEvent('tool_activity', { tool: event.data.tool_name, phase: event.data.phase });
      const { tool_name, phase, summary } = event.data;
      if (phase === "start") {
        activeToolRef.current = ({ name: tool_name, summary: summary || "" });
        toolStartTimeRef.current = (Date.now());
      } else if (phase === "end") {
        activeToolRef.current = (null);
        toolStartTimeRef.current = (null);
      }
      return;
    }

    // permission_request / permission_resolved: now handled via DB interactive
    // cards — WS events trigger a display file refresh so the card appears.
    if (event.type === "permission_request" || event.type === "permission_resolved") {
      refreshMessagesRef.current({ syncHint: true });
      return;
    }

    if (event.type === "new_message") {
      pushWsEvent('new_message', event.data);
      const hasActiveCompact = activeToolRef.current?.name === "Compact";
      if (hasActiveCompact) {
        refreshMessagesRef.current({ syncHint: true });
        return;
      }
      activeToolRef.current = (null);
      toolStartTimeRef.current = (null);
      refreshMessagesRef.current({ syncHint: event.data?.message_id === "sync" });
      return;
    }

    // Chat-message signals — display file is the single source of truth.
    // Each signal routes to the channel(s) it affects:
    //   pre_sent_*   → preSent channel only (queued/scheduled/cancelled changes)
    //   message_sent → BOTH (promote evicts from preSent, inserts into sent)
    //   delivered/executed/update/metadata_update → sent channel only
    const PRE_SENT_SIGNALS = new Set([
      "pre_sent_created", "pre_sent_updated", "pre_sent_tombstoned",
    ]);
    const SENT_SIGNALS = new Set([
      "message_executed", "message_delivered", "message_update",
      "metadata_update",
    ]);
    if (event.type === "message_sent") {
      pushWsEvent(event.type, { id: event.data?.message_id });
      debouncedRefreshSent();
      debouncedRefreshPreSent();
      return;
    }
    if (PRE_SENT_SIGNALS.has(event.type)) {
      pushWsEvent(event.type, { id: event.data?.message_id });
      debouncedRefreshPreSent();
      return;
    }
    if (SENT_SIGNALS.has(event.type)) {
      pushWsEvent(event.type, { id: event.data?.message_id });
      debouncedRefreshSent();
      return;
    }

    if (event.type === "agent_update") {
      console.log('[ws] agent_update', event.data.agent_id?.slice(0,8), event.data.status);
      pushWsEvent('agent_update', { status: event.data.status });
      const status = event.data.status;
      // Update agent status directly from WS — no wait for REST poll
      if (event.data.agent_id === id) {
        const patch = { status };
        if ("insight_status" in event.data) {
          patch.insight_status = event.data.insight_status || null;
        }
        if ("has_pending_suggestions" in event.data) {
          patch.has_pending_suggestions = !!event.data.has_pending_suggestions;
        }
        if ("deferred_to" in event.data) {
          patch.deferred_to = event.data.deferred_to || null;
          setDeferredTo(event.data.deferred_to || null);
        }
        if ("muted" in event.data) {
          patch.muted = !!event.data.muted;
          setAgentMuted(id, !!event.data.muted);
        }
        if ("name" in event.data && event.data.name) {
          patch.name = event.data.name;
        }
        setAgent((prev) => prev ? { ...prev, ...patch } : prev);
      }
      if (status !== "EXECUTING" && status !== "IDLE") {
        activeToolRef.current = (null);
        toolStartTimeRef.current = (null);
        generationIdRef.current = null;
      }
      refreshMessagesRef.current();
    }

    if (event.type === "progress_suggestions_ready") {
      if (event.data.agent_id === id) {
        setAgent((prev) => prev ? { ...prev, has_pending_suggestions: true, insight_status: null } : prev);
      }
      // No loadData() — agent state is already patched and the suggestion
      // rows are fetched by ProgressSuggestionsCard on mount. A full refetch
      // here used to clobber the just-patched state with a stale brief.
      showToast("Insights ready for review");
    }

  }, [id, agent?.project, agent?.session_id, agent?.id]);

  // Debug: POST rendered message state + DOM elements to backend every 1s
  // Enable via localStorage: localStorage.setItem("ah:debug-frontend-state", "1")
  useEffect(() => {
    if (!id || !messages?.length) return;
    if (localStorage.getItem("ah:debug-frontend-state") !== "1") return;
    const timer = setInterval(() => {
      // Scan DOM for actual rendered message elements
      const domElements = [];
      const container = document.querySelector('[data-chat-container]');
      if (container) {
        const containerRect = container.getBoundingClientRect();
        container.querySelectorAll('[data-msg-id]').forEach(el => {
          const rect = el.getBoundingClientRect();
          domElements.push({
            msgId: el.dataset.msgId,
            type: el.dataset.msgType || '?',
            y: Math.round(rect.top - containerRect.top),
            h: Math.round(rect.height),
            visible: rect.top < window.innerHeight && rect.bottom > 0,
            text: el.textContent?.slice(0, 80)?.replace(/\n/g, ' '),
          });
        });
      }
      const snapshot = {
        agentId: id,
        page: 'AgentChatPage',
        messages: messages.map(m => ({
          id: m.id, role: m.role, kind: m.kind,
          session_seq: m.session_seq, source: m.source,
          created_at: m.created_at,
          content: (m.content || '').slice(0, 100),
        })),
        domElements,
        wsEvents: wsEventLog.current.slice(),
      };
      fetch('/api/debug/frontend-state', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(snapshot),
      }).catch(() => {});  // fire and forget
    }, 1000);
    return () => clearInterval(timer);
  }, [id, messages]);

  // Auto-select name input when rename starts (useEffect runs after DOM commit)
  useEffect(() => {
    if (editingName) nameInputRef.current?.select();
  }, [editingName]);

  // Rename agent
  const startRename = () => {
    setNameDraft(agent?.name || "");
    setEditingName(true);
  };
  const submitRename = async () => {
    const trimmed = nameDraft.trim();
    if (!trimmed || trimmed === agent?.name) {
      setEditingName(false);
      return;
    }
    try {
      await renameAgent(id, trimmed);
      setAgent((prev) => prev ? { ...prev, name: trimmed } : prev);
      // Keep the brief cache in sync so list pages (AgentsPage,
      // ProjectDetailPage) don't render the old name from cache.
      const cached = agentBriefCache.get(id);
      if (cached) agentBriefCache.set(id, { ...cached, name: trimmed });
      window.dispatchEvent(new CustomEvent("agent-renamed", { detail: { agentId: id, name: trimmed } }));
      showToast("Renamed");
    } catch (err) {
      showToast("Rename failed: " + err.message, "error");
    }
    setEditingName(false);
  };

  // Send message — backend decides tmux-immediate (QUEUED) vs PENDING
  const handleSend = async (content) => {
    try {
      activeToolRef.current = (null);
      toolStartTimeRef.current = (null);
      await sendMessage(id, content);
      refreshMessages();
    } catch (err) {
      setFailedMessages((prev) => [...prev, {
        id: `failed-${Date.now()}-${Math.random().toString(36).slice(2, 6)}`,
        content,
        created_at: new Date().toISOString(),
        error: err.message || "Send failed",
      }]);
    }
  };

  // Send later (queued with scheduled_at)
  const handleSendLater = async (content, scheduledAt) => {
    try {
      await sendMessage(id, content, { scheduled_at: scheduledAt });
      const when = new Date(scheduledAt);
      const timeStr = when.toLocaleTimeString([], TIME_SHORT);
      showToast(`Scheduled for ${timeStr}`);
      loadData();
    } catch (err) {
      showToast("Failed: " + err.message, "error");
      throw err;
    }
  };

  // Per-message DELETE button: tombstones the bubble. Hits both states
  // since the message could be pre-sent or sent (for sent, the backend
  // tombstones the display entry). WS pre_sent_tombstoned will re-sync
  // pre-sent state; we optimistically drop from both source states for
  // immediate visual feedback.
  const handleCancelMessage = async (messageId) => {
    try {
      await deleteMessage(id, messageId);
      setPreSentMessages((prev) => prev.filter((m) => m.id !== messageId));
      setSentMessages((prev) => prev.filter((m) => m.id !== messageId));
      showToast("Message deleted");
    } catch (err) {
      showToast("Failed: " + err.message, "error");
    }
  };

  // Update a scheduled/pending message — always pre-sent.
  const handleUpdateMessage = async (messageId, data) => {
    try {
      const updated = await updateMessage(id, messageId, data);
      setPreSentMessages((prev) => prev.map((m) => (m.id === messageId ? updated : m)));
      showToast("Message updated");
    } catch (err) {
      showToast("Failed: " + err.message, "error");
    }
  };

  // Composer-based edit flow for queued/scheduled messages.
  const handleStartEditMessage = (message) => {
    setEditingMessage({ id: message.id, content: message.content });
  };
  const handleSaveMessageEdit = async (content) => {
    if (!editingMessage) return;
    const { id: msgId, content: original } = editingMessage;
    setEditingMessage(null);
    if (content !== original) await handleUpdateMessage(msgId, { content });
  };
  const handleCancelMessageEdit = () => setEditingMessage(null);

  const handleRetryFailed = async (failedId) => {
    const msg = failedMessages.find((m) => m.id === failedId);
    if (!msg) return;
    setFailedMessages((prev) => prev.filter((m) => m.id !== failedId));
    await handleSend(msg.content);
  };

  const handleDismissFailed = (failedId) => {
    setFailedMessages((prev) => prev.filter((m) => m.id !== failedId));
  };

  // Send a scheduled message immediately — pre-sent transition (still
  // pre-sent, just status changes). Refresh pre-sent for canonical state.
  const handleSendNow = async (messageId) => {
    try {
      const updated = await updateMessage(id, messageId, { scheduled_at: "" });
      setPreSentMessages((prev) => prev.map((m) => (m.id === messageId ? updated : m)));
      showToast("Sending now");
    } catch (err) {
      showToast("Failed: " + err.message, "error");
    }
  };

  // Stop agent
  const handleStop = async () => {
    setStopping(true);
    try {
      // Build feedback text with any attached files
      let feedbackText = incompleteReason.trim();
      const uploaded = feedbackAttachments.filter((a) => a.uploadedPath);
      if (uploaded.length > 0) {
        for (const a of uploaded) feedbackText += `\n[Attached file: ${a.uploadedPath}]`;
      }
      await stopAgent(id, {
        generateSummary: generateSummary && !!agent?.task_id,
        taskComplete: agent?.task_id ? taskOutcome === "complete" : true,
        taskDrop: taskOutcome === "drop",
        incompleteReason: (taskOutcome !== "complete" && feedbackText) ? feedbackText : null,
      });
      const label = !agent?.task_id
        ? "Agent stopped"
        : taskOutcome === "complete"
          ? "Agent stopped — task complete"
          : taskOutcome === "drop"
            ? "Agent stopped — task dropped"
            : "Agent stopped — task returned to inbox";
      showToast(generateSummary && agent?.task_id ? label + " (generating insights...)" : label);
      loadData();
      window.dispatchEvent(new CustomEvent("agents-data-changed"));
    } catch (err) {
      showToast("Failed: " + err.message, "error");
    } finally {
      setStopping(false);
      setShowStopConfirm(false);
      setTaskOutcome("complete");
      setIncompleteReason("");
      clearFeedbackAttachments();
    }
  };

  // Resume agent — always uses tmux
  const handleResume = async () => {
    setResuming(true);
    try {
      // All agents resume as tmux sessions
      const mode = !agent?.successor_id ? "tmux" : null;
      await resumeAgent(id, mode ? { mode } : null);
      showToast("Agent resumed");
      loadData();
      window.dispatchEvent(new CustomEvent("agents-data-changed"));
    } catch (err) {
      // Handle superseded agent (409)
      try {
        const info = JSON.parse(err.message);
        if (info.reason === "superseded") {
          showToast("This agent was continued by a successor", "error");
          loadData(); // refresh to pick up successor_id
          return;
        }
      } catch {}
      showToast("Failed: " + err.message, "error");
    } finally {
      setResuming(false);
    }
  };

  // Agent status is written directly by backend hooks — no frontend derivation needed.
  // Smooth EXECUTING→off: hold true for 1s to avoid flicker
  // (must be before early returns to maintain hooks ordering)
  const isExecutingRaw = agent?.status === "EXECUTING";
  const [isExecuting, setIsExecuting] = useState(isExecutingRaw);
  const execTimerRef = useRef(null);
  useEffect(() => {
    if (isExecutingRaw) {
      clearTimeout(execTimerRef.current);
      setIsExecuting(true);
    } else {
      execTimerRef.current = setTimeout(() => setIsExecuting(false), 1000);
    }
    return () => clearTimeout(execTimerRef.current);
  }, [isExecutingRaw]);

  // Full-page skeleton only when we don't even have brief metadata yet.
  // If `agent` is seeded from the brief cache, fall through and render
  // the real layout — header / composer paint at once and the message
  // area gets its own inline skeleton (see below) until messages arrive.
  if (loading && !agent) {
    return <ChatSkeleton />;
  }

  if (!agent) {
    return (
      <div className="flex flex-col items-center justify-center py-20 text-faint">
        <p>Agent not found</p>
        <button type="button" onClick={() => { if (onClose) onClose(); else navigate("/agents"); }} className="mt-2 text-sm text-accent underline">
          Back to Agents
        </button>
      </div>
    );
  }

  const isHealthy = isSystemHealthy(health);
  // Memory pressure flag — orchestrator + children RSS. Same thresholds as
  // PageHeader so the pill behaviour is consistent across the app.
  const _memMb = sysStats?.xylocopa?.mem_mb;
  const _memHigh = typeof _memMb === "number" && _memMb > 5000;
  const _memWarn = typeof _memMb === "number" && _memMb > 1000;
  const _memLabel = typeof _memMb === "number"
    ? _memMb >= 1024 ? `${(_memMb / 1024).toFixed(1)} GB` : `${Math.round(_memMb)} MB`
    : null;
  let healthChipCls, healthDotColor, healthLabel, healthTitle;
  if (health === null) {
    healthChipCls = "bg-gray-500/15 text-gray-400";
    healthDotColor = "bg-gray-400";
    healthLabel = "...";
    healthTitle = "Checking...";
  } else if (!isHealthy) {
    healthChipCls = "danger-tint-15 text-danger";
    healthDotColor = "bg-danger";
    healthLabel = "Error";
    healthTitle = "System issue";
  } else if (_memHigh) {
    healthChipCls = "danger-tint-15 text-danger";
    healthDotColor = "bg-danger";
    healthLabel = `Mem ${_memLabel}`;
    healthTitle = `High memory: ${_memLabel} (steady ~200 MB)`;
  } else if (_memWarn) {
    healthChipCls = "bg-amber-500/15 text-amber-500";
    healthDotColor = "bg-amber-500";
    healthLabel = `Mem ${_memLabel}`;
    healthTitle = `Elevated memory: ${_memLabel}`;
  } else {
    healthChipCls = "ok-tint-15 text-ok";
    healthDotColor = "bg-ok";
    healthLabel = "OK";
    healthTitle = "System healthy";
  }
  const _healthPulse = (!isHealthy && health !== null) || _memHigh;

  // Status is authoritative from backend — read directly
  const statusDot = AGENT_STATUS_COLORS[agent.status] || "bg-gray-500";
  const statusText = AGENT_STATUS_TEXT_COLORS[agent.status] || "text-dim";
  const isIdle = agent.status === "IDLE";
  const hasTmux = isIdle && !!agent.tmux_pane;
  const hasTmuxPane = !!agent.tmux_pane;
  const isStopped = agent.status === "STOPPED";
  const isError = agent.status === "ERROR";
  const isStarting = agent.status === "STARTING";
  const compactHeader = embedded && !headerExpanded;

  let disabledReason = "";
  if (isStarting) disabledReason = "Agent is starting…";
  else if (isStopped) disabledReason = "Agent is stopped — click Resume to restart";
  else if (isError) disabledReason = "Agent errored — click Resume to restart";

  return (
    <div ref={kbContainerRef} className="flex flex-col h-full relative">

      {/* Header */}
      <div className={`shrink-0 bg-surface border-b border-divider px-4 ${compactHeader ? "py-1.5" : "py-2"} safe-area-pt relative z-10`}>
        <div className={`${embedded ? "" : "max-w-2xl"} mx-auto ${compactHeader ? "" : "space-y-1.5"}`}>
          {/* Row 1: Back + name | project + icon buttons */}
          <div className="flex items-center gap-2">
            <button
              type="button"
              onClick={() => {
                const back = resolveBack(location.pathname, location.state);
                navigate(back.to, { state: back.state });
              }}
              className="shrink-0 w-7 h-7 flex items-center justify-center rounded-lg hover:bg-input transition-colors"
            >
              <svg className="w-5 h-5 text-label" fill="none" stroke="currentColor" strokeWidth={2} viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" d="M15 19l-7-7 7-7" />
              </svg>
            </button>

            {editingName ? (
              <input
                ref={nameInputRef}
                value={nameDraft}
                onChange={(e) => setNameDraft(e.target.value)}
                onBlur={submitRename}
                onKeyDown={(e) => {
                  if (e.key === "Enter") submitRename();
                  if (e.key === "Escape") setEditingName(false);
                }}
                maxLength={200}
                className="text-sm font-semibold text-heading min-w-0 flex-1 bg-input border border-accent rounded px-1.5 py-0.5 outline-none"
              />
            ) : (
              <div className="min-w-0 flex-1 flex items-center gap-2">
                {/* Status dot — left of title, matches ProjectsPage running-pulse style */}
                <span
                  className={`shrink-0 w-1.5 h-1.5 rounded-full ${
                    agent.status === "EXECUTING" ? "bg-accent animate-glow"
                      : agent.status === "IDLE" ? "bg-green-500/50"
                      : agent.status === "ERROR" ? "bg-red-400"
                      : "bg-zinc-400/50"
                  }`}
                  title={agent.status.toLowerCase().replace("_", " ")}
                />
                <h1
                  onDoubleClick={startRename}
                  title="Double-tap to rename"
                  className="text-sm font-semibold text-heading truncate min-w-0 select-none"
                >
                  {agent.name}
                </h1>
              </div>
            )}

            {compactHeader ? (
              /* Compact: stop/resume + expand chevron */
              <div className="shrink-0 flex items-center gap-1.5">
                {(isStopped || isError) ? (
                  <button type="button" onClick={() => handleResume()} disabled={resuming}
                    className="px-2 py-0.5 flex items-center gap-0.5 rounded-full text-[10px] font-medium accent-tint-15 text-accent transition-colors disabled:opacity-50 enabled:hover:accent-tint-25">
                    <svg className="w-2.5 h-2.5" fill="currentColor" viewBox="0 0 24 24"><path d="M6 4l14 8-14 8V4z" /></svg>
                    {resuming ? "..." : "Resume"}
                  </button>
                ) : (
                  <button type="button" onClick={() => setShowStopConfirm(true)}
                    className="px-2 py-0.5 flex items-center gap-0.5 rounded-full text-[10px] font-medium danger-tint-15 text-danger transition-colors hover:bg-red-500/25">
                    <svg className="w-2.5 h-2.5" fill="currentColor" viewBox="0 0 24 24"><rect x="6" y="6" width="12" height="12" rx="2" /></svg>
                    Stop
                  </button>
                )}
                <button type="button" onClick={() => setHeaderExpanded(true)} title="Show details"
                  className="w-6 h-6 flex items-center justify-center rounded-lg text-dim hover:text-body hover:bg-input transition-colors">
                  <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" strokeWidth={2} viewBox="0 0 24 24">
                    <path strokeLinecap="round" strokeLinejoin="round" d="M19 9l-7 7-7-7" />
                  </svg>
                </button>
              </div>
            ) : (
              /* Full: action buttons (Stop / Resume / Continued / OK) — promoted to row 1 */
              <div className="shrink-0 flex items-center gap-1">
                {(isStopped || isError) && agent?.successor_id && (
                  <button
                    type="button"
                    onClick={() => embedded && onNavigateAgent ? onNavigateAgent(agent.successor_id) : navigate(`/agents/${agent.successor_id}`, { state: forwardState(location) })}
                    className="px-2 py-0.5 flex items-center gap-0.5 rounded-full text-[10px] font-medium bg-violet-600 hover:bg-violet-500 text-white transition-colors"
                  >
                    Continued
                    <svg className="w-2.5 h-2.5" fill="none" stroke="currentColor" strokeWidth={2.5} viewBox="0 0 24 24">
                      <path strokeLinecap="round" strokeLinejoin="round" d="M13.5 4.5L21 12m0 0l-7.5 7.5M21 12H3" />
                    </svg>
                  </button>
                )}
                {(isStopped || isError) ? (
                  <button
                    type="button"
                    onClick={() => handleResume()}
                    disabled={resuming}
                    className="px-2 py-0.5 flex items-center gap-0.5 rounded-full text-[10px] font-medium accent-tint-15 text-accent transition-colors disabled:opacity-50 enabled:hover:accent-tint-25"
                  >
                    <svg className="w-2.5 h-2.5" fill="currentColor" viewBox="0 0 24 24">
                      <path d="M6 4l14 8-14 8V4z" />
                    </svg>
                    {resuming ? "..." : "Resume"}
                  </button>
                ) : (
                  <button
                    type="button"
                    onClick={() => setShowStopConfirm(true)}
                    className="px-2 py-0.5 flex items-center gap-0.5 rounded-full text-[10px] font-medium danger-tint-15 text-danger transition-colors hover:bg-red-500/25"
                  >
                    <svg className="w-2.5 h-2.5" fill="currentColor" viewBox="0 0 24 24">
                      <rect x="6" y="6" width="12" height="12" rx="2" />
                    </svg>
                    Stop
                  </button>
                )}

                <ContextUsagePill usage={contextUsage} agentId={id} />

                <button
                  type="button"
                  onClick={() => navigate("/monitor")}
                  title={healthTitle}
                  className={`inline-flex items-center gap-1 px-1.5 py-0.5 rounded-full text-[10px] font-medium transition-colors hover:opacity-80 ${healthChipCls}`}
                >
                  <span className={`inline-block w-1.5 h-1.5 rounded-full ${healthDotColor} ${_healthPulse ? "animate-pulse" : ""}`} />
                  {healthLabel}
                </button>

                {embedded && (
                <button type="button" onClick={() => setHeaderExpanded(false)} title="Collapse"
                  className="w-7 h-7 flex items-center justify-center rounded-lg text-dim hover:text-body hover:bg-input transition-colors">
                  <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" strokeWidth={2} viewBox="0 0 24 24">
                    <path strokeLinecap="round" strokeLinejoin="round" d="M5 15l7-7 7 7" />
                  </svg>
                </button>
                )}
              </div>
            )}
          </div>


          {/* Row 2: Status dot + tags (card-list style) | action buttons (ml-9 aligns with name after back btn) */}
          {!compactHeader && <div className="flex items-center gap-2 ml-9">
            <div className="flex items-center gap-1 min-w-0 flex-1 overflow-x-auto no-scrollbar">
              {agent.project && (
                <span
                  className="shrink-0 text-[10px] font-medium px-1.5 py-0.5 rounded-full accent-tint-15 text-accent max-w-[140px] truncate cursor-pointer hover:accent-tint-25 transition-colors"
                  onClick={() => navigate(`/projects/${encodeURIComponent(agent.project)}`)}
                  title={agent.project}
                >
                  {agent.project}
                </span>
              )}
              {agent.worktree && <WorktreePill name={agent.worktree} padY="py-0.5" />}
              {agent.skip_permissions && (
                <span className="shrink-0 text-[10px] font-medium px-1.5 py-0.5 rounded-full bg-amber-500/15 text-amber-500 dark:text-amber-400">
                  Auto
                </span>
              )}
              {agent.task_id && (
                <button
                  type="button"
                  onClick={() => setShowTaskCard(true)}
                  title="View task"
                  className="shrink-0 text-[10px] font-medium px-1.5 py-0.5 rounded-full bg-orange-500/15 text-orange-600 dark:text-orange-400 hover:bg-orange-500/25 transition-colors"
                >
                  Task
                </button>
              )}
              <span className="shrink-0 inline-flex items-center">
                <button
                  ref={idPillRef}
                  type="button"
                  onPointerEnter={(e) => {
                    if (e.pointerType !== "mouse") return;
                    idCancelHoverClose();
                    const rect = idPillRef.current?.getBoundingClientRect();
                    if (rect) setIdPopoverPos({ top: rect.bottom, left: rect.left + rect.width / 2 });
                    setShowIdPopover(true);
                  }}
                  onPointerLeave={(e) => {
                    if (e.pointerType !== "mouse") return;
                    idScheduleHoverClose();
                  }}
                  onPointerDown={(e) => {
                    if (e.pointerType === "mouse") return;
                    idLongPressFiredRef.current = false;
                    idPressTimerRef.current = setTimeout(() => {
                      idPressTimerRef.current = null;
                      idLongPressFiredRef.current = true;
                      const rect = idPillRef.current?.getBoundingClientRect();
                      if (rect) setIdPopoverPos({ top: rect.bottom, left: rect.left + rect.width / 2 });
                      setShowIdPopover(true);
                    }, LONG_PRESS_DELAY);
                  }}
                  onPointerUp={(e) => {
                    if (e.pointerType === "mouse") return;
                    if (idPressTimerRef.current) {
                      clearTimeout(idPressTimerRef.current);
                      idPressTimerRef.current = null;
                    }
                  }}
                  onPointerCancel={() => {
                    if (idPressTimerRef.current) {
                      clearTimeout(idPressTimerRef.current);
                      idPressTimerRef.current = null;
                    }
                  }}
                  onClick={(e) => {
                    e.stopPropagation();
                    // Swallow the click that follows a long-press
                    if (idLongPressFiredRef.current) {
                      idLongPressFiredRef.current = false;
                    }
                  }}
                  onDoubleClick={(e) => {
                    e.stopPropagation();
                    setShowIdPopover(false);
                    navigator.clipboard.writeText(agent.id).then(() => {
                      showToast("Copied " + agent.id);
                    }).catch(() => {});
                  }}
                  onContextMenu={(e) => e.preventDefault()}
                  style={{ touchAction: "manipulation", userSelect: "none", WebkitUserSelect: "none", WebkitTouchCallout: "none" }}
                  className="text-[10px] font-medium px-1.5 py-0.5 rounded-full bg-elevated text-dim hover:text-body hover:bg-input transition-colors"
                >
                  {agent.id.slice(0, 4)}
                </button>
                {showIdPopover && idPopoverPos && createPortal(
                  <div
                    ref={idPopoverRef}
                    className="fixed z-[61]"
                    style={{ top: idPopoverPos.top, left: idPopoverPos.left, transform: "translateX(-50%)", paddingTop: 6 }}
                    onPointerEnter={idCancelHoverClose}
                    onPointerLeave={(e) => { if (e.pointerType === "mouse") idScheduleHoverClose(); }}
                  >
                    <div className="relative">
                      <div className="px-2 py-1.5 rounded-lg bg-surface border border-divider shadow-popover flex items-center gap-2 whitespace-nowrap">
                        <span className="text-[11px] text-dim">xylo id:</span>
                        <span className="text-[11px] font-mono text-body select-all">{agent.id}</span>
                        <button
                          type="button"
                          onClick={(e) => {
                            e.stopPropagation();
                            navigator.clipboard.writeText(agent.id).then(() => {
                              showToast("Copied " + agent.id);
                              setShowIdPopover(false);
                            }).catch(() => {});
                          }}
                          className="text-[10px] text-accent hover:underline"
                        >
                          Copy
                        </button>
                      </div>
                      <PopoverArrow size={8} />
                    </div>
                  </div>,
                  document.body
                )}
              </span>
            </div>

            {/* Icon toolbar — moved down from row 1 */}
            <div className="shrink-0 flex items-center -mr-1">
              <button
                type="button"
                onClick={() => setShowTerminal(true)}
                title="Terminal (attach to tmux session)"
                className="shrink-0 w-7 h-7 flex items-center justify-center rounded-lg text-zinc-400 hover:text-zinc-300 hover:bg-input transition-colors"
              >
                <SquareTerminal className="w-4 h-4" strokeWidth={1.75} />
              </button>
              <button
                type="button"
                onClick={async () => {
                  if (syncRefreshing) return;
                  setSyncRefreshing(true);
                  try {
                    await wakeSync(id);
                    setTimeout(() => refreshMessages(), SYNC_SETTLE_DELAY);
                  } catch {}
                  setTimeout(() => setSyncRefreshing(false), 400);
                }}
                title="Refresh (incremental sync)"
                className="shrink-0 w-7 h-7 flex items-center justify-center rounded-lg text-zinc-400 hover:text-zinc-300 hover:bg-input transition-colors"
              >
                <svg className={`w-4 h-4 ${syncRefreshing ? "animate-spin-reverse" : ""}`} fill="none" stroke="currentColor" strokeWidth={1.75} viewBox="0 0 24 24">
                  <path strokeLinecap="round" strokeLinejoin="round" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" />
                </svg>
              </button>
              <button
                type="button"
                onClick={() => setShowBrowser(true)}
                title="Browse files"
                className="shrink-0 w-7 h-7 flex items-center justify-center rounded-lg text-zinc-400 hover:text-zinc-300 hover:bg-input transition-colors"
              >
                <svg className="w-4 h-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.75}>
                  <path strokeLinecap="round" strokeLinejoin="round" d="M3 7v10a2 2 0 002 2h14a2 2 0 002-2V9a2 2 0 00-2-2h-6l-2-2H5a2 2 0 00-2 2z" />
                </svg>
              </button>
              <button
                type="button"
                onClick={handleToggleMute}
                title={muted ? "Unmute notifications" : "Mute notifications"}
                className="w-7 h-7 flex items-center justify-center rounded-lg hover:bg-input transition-colors"
              >
                {muted ? (
                  <BellOff className="w-4 h-4 text-zinc-400 hover:text-zinc-300 transition-colors" strokeWidth={1.75} />
                ) : (
                  <Bell className="w-4 h-4 text-accent" strokeWidth={1.75} />
                )}
              </button>

              <div className="relative">
                <button
                  type="button"
                  onClick={() => setShowDeferPicker(v => !v)}
                  title={deferredTo
                    ? `Hidden until ${new Date(deferredTo).toLocaleString()}`
                    : "Defer (hide until later)"}
                  className="w-7 h-7 flex items-center justify-center rounded-lg hover:bg-input transition-colors"
                >
                  <Hourglass
                    className={`w-4 h-4 transition-colors ${
                      deferredTo ? "text-indigo-400" : "text-zinc-400 hover:text-zinc-300"
                    }`}
                    strokeWidth={1.75}
                  />
                </button>
                {showDeferPicker && (
                  <SendLaterPicker
                    title="Defer Until"
                    onSelect={handleDeferSelect}
                    onClose={() => setShowDeferPicker(false)}
                    onClear={deferredTo ? handleDeferClear : undefined}
                  />
                )}
              </div>

              <button
                type="button"
                onClick={handleToggleStar}
                disabled={starLoading}
                title={starred ? "Unstar session" : "Star session"}
                className="w-7 h-7 flex items-center justify-center rounded-lg hover:bg-input transition-colors disabled:opacity-50"
              >
                {starred ? (
                  <svg className="w-4 h-4 text-amber-400" fill="currentColor" viewBox="0 0 24 24">
                    <path d="M12 2l3.09 6.26L22 9.27l-5 4.87 1.18 6.88L12 17.77l-6.18 3.25L7 14.14 2 9.27l6.91-1.01L12 2z" />
                  </svg>
                ) : (
                  <svg className="w-4 h-4 text-zinc-400 hover:text-zinc-300 transition-colors" fill="none" stroke="currentColor" strokeWidth={1.75} viewBox="0 0 24 24">
                    <path strokeLinecap="round" strokeLinejoin="round" d="M12 2l3.09 6.26L22 9.27l-5 4.87 1.18 6.88L12 17.77l-6.18 3.25L7 14.14 2 9.27l6.91-1.01L12 2z" />
                  </svg>
                )}
              </button>

            </div>
          </div>}
        </div>
      </div>

      {/* Messages */}
      <div
        ref={scrollContainerRef}
        data-chat-container
        onScroll={handleScroll}
        className={`flex-1 overflow-y-auto overflow-x-hidden px-4 py-3 ${kbOpen ? "" : "pb-36"} ${embedded ? "" : "max-w-2xl"} mx-auto w-full flex flex-col`}
        style={{ overflowAnchor: "auto", overscrollBehavior: "contain" }}
      >
        <div className="mt-auto" />
        {loading && messages.length === 0 ? (
          // Header rendered already (agent seeded from brief cache); leave
          // the scroll area blank until /display/sent + /display/pre-sent
          // resolve — no placeholder bubbles.
          null
        ) : messages.length === 0 && agent.status === "STARTING" ? (
          <InitializingIndicator />
        ) : messages.length === 0 && agent.status === "IDLE" ? (
          <SyncPrompt agentId={id} onSync={refreshMessages} />
        ) : (
          <>
            {/* Lazy-load indicator at top of scroll container.
                The "Attempts" retry card now lives in the message stream
                itself as a seq=0 retry_marker entry (intercepted in the
                visible.map below), so it's only visible when the user
                has lazy-loaded all the way back — matching the natural
                position of "first thing said in this conversation". */}
            {loadingMore && (
              <div className="text-center py-3 text-xs opacity-60">Loading older messages...</div>
            )}
            {!hasMore && messages.length > 0 && (
              <div className="text-center py-3 text-xs opacity-40">Beginning of conversation</div>
            )}

            {(() => {
              // Source the main scroll directly from sentMessages — pre-sent
              // entries live in their own state and render in the bottom
              // queue area. We still filter out stop_hook entries (system
              // bookkeeping not meant for the chat view).
              const isRetryAgent = (taskData?.attempt_agents || []).findIndex(a => a.agent_id === id) >= 1;
              const retryContext = taskData?.retry_context;
              let replacedFirstTask = false;
              const visible = sentMessages.filter((m) => m.kind !== "stop_hook")
                .map((m) => {
                  // For retry agents, replace first source=task user message text with retry_context,
                  // but keep all [Attached file: ...] tags from both original and retry_context
                  if (isRetryAgent && retryContext && !replacedFirstTask && m.role === "USER" && m.source === "task") {
                    replacedFirstTask = true;
                    const origAttachments = (m.content || "").match(/\[Attached file: [^\]]+\]/g) || [];
                    const retryText = retryContext.replace(/^User feedback:\s*/i, "");
                    const retryAttachments = retryText.match(/\[Attached file: [^\]]+\]/g) || [];
                    const cleanText = retryText.replace(/\[Attached file: [^\]]+\]/g, "").trim();
                    const allAttachments = [...new Set([...retryAttachments, ...origAttachments])];
                    return { ...m, content: cleanText + (allAttachments.length ? "\n" + allAttachments.join("\n") : "") };
                  }
                  return m;
                });
              // Build tool groups: consecutive tool_use + tool_activity messages get merged
              const toolGroups = new Map(); // first msg id -> [entries]
              let groupStart = null;
              for (let i = 0; i < visible.length; i++) {
                const m = visible[i];
                const isToolUse = m.role === "AGENT" && m.kind === "tool_use";
                const isToolActivity = m.kind === "tool_activity";
                if (isToolUse || isToolActivity) {
                  if (!groupStart) groupStart = m.id;
                  if (!toolGroups.has(groupStart)) toolGroups.set(groupStart, []);
                  // Skip tool_activity start-phase entries (duplicates of end-phase)
                  if (isToolActivity && (m.metadata?.phase) === "start") continue;
                  if (isToolUse) {
                    const match = (m.content || "").match(TOOL_SUMMARY_RE);
                    toolGroups.get(groupStart).push({
                      name: match ? match[1] : "Tool",
                      summary: match ? match[2] : m.content || "",
                    });
                  } else {
                    const meta = m.metadata || {};
                    toolGroups.get(groupStart).push({
                      name: meta.tool_name || "Tool",
                      summary: m.content || "",
                    });
                  }
                } else {
                  groupStart = null;
                }
              }
              // Map each text message to preceding tool entries for media extraction
              const toolEntriesForText = new Map();
              {
                let acc = [];
                for (const m of visible) {
                  const isToolMsg = (m.role === "AGENT" && m.kind === "tool_use") || m.kind === "tool_activity";
                  if (isToolMsg) {
                    const ge = toolGroups.get(m.id);
                    if (ge) acc.push(...ge);
                  } else if (m.role === "AGENT" && m.kind === "text") {
                    if (acc.length > 0) { toolEntriesForText.set(m.id, acc); acc = []; }
                  } else {
                    acc = [];
                  }
                }
              }
              return visible.map((msg) => {
                // Retry marker (seq=0): orange "Attempts" card with pills.
                // Sourced from the in-message metadata (attempts list +
                // current_idx); description / agent_summary / retry_context
                // remain live from taskData since they can change after the
                // marker was written (e.g. summary regeneration).
                if (msg.kind === "retry_marker") {
                  const meta = msg.metadata || {};
                  const attempts = meta.attempts || [];
                  const myIdx = meta.current_idx ?? 0;
                  const total = attempts.length;
                  const sel = selectedAttempt ?? myIdx;
                  const selAgent = attempts[sel];
                  const isSelCurrent = selAgent?.agent_id === id;
                  const showSummary = sel < total - 1 && sel === total - 2 && taskData?.agent_summary;
                  const showUserFeedback = sel < total - 1 && sel === total - 2 && taskData?.retry_context;
                  return (
                    <div key={msg.id} data-msg-id={msg.id} data-msg-type="retry_marker" className="mx-auto w-[85%] mb-3 rounded-xl bg-orange-500/8 border border-orange-500/20 px-4 py-3">
                      <div className="flex items-center gap-1.5 mb-2">
                        <span className="text-[10px] font-semibold text-orange-600 dark:text-orange-400 mr-1">Attempts</span>
                        <div className="flex flex-wrap items-center gap-1.5">
                          {attempts.map((a, i) => (
                            <button
                              key={a.agent_id}
                              type="button"
                              onClick={() => setSelectedAttempt(i)}
                              className={`px-2 py-0.5 rounded-full text-[10px] font-medium transition-colors ${
                                i === sel
                                  ? "bg-orange-500 text-white"
                                  : "bg-transparent border border-orange-500/40 text-orange-600 dark:text-orange-400 hover:bg-orange-500/15"
                              }`}
                            >
                              #{i + 1}
                            </button>
                          ))}
                        </div>
                        {!isSelCurrent && selAgent && (
                          <button
                            type="button"
                            onClick={() => embedded && onNavigateAgent ? onNavigateAgent(selAgent.agent_id) : navigate(`/agents/${selAgent.agent_id}`, { state: forwardState(location) })}
                            className="ml-auto text-[10px] text-orange-600 dark:text-orange-400 hover:underline"
                          >
                            Enter Chat →
                          </button>
                        )}
                      </div>
                      {taskData?.description && (
                        <p className="text-xs text-dim/80 whitespace-pre-wrap">{taskData.description.replace(/\[Attached file: [^\]]+\]/g, "").trim()}</p>
                      )}
                      {showSummary && (
                        <details className="mt-2">
                          <summary className="text-[10px] font-medium text-orange-600 dark:text-orange-400 cursor-pointer select-none list-none flex items-center gap-1">
                            <span className="transition-transform duration-200 text-[8px] [details[open]>&]:rotate-90">▶</span>
                            Agent Summary
                          </summary>
                          <div className="mt-1">
                            {taskData.agent_summary === ":::generating:::" ? (
                              <p className="text-xs text-dim/50 italic">Generating summary...</p>
                            ) : (
                              <p className="text-xs text-dim/80 whitespace-pre-wrap">{taskData.agent_summary}</p>
                            )}
                          </div>
                        </details>
                      )}
                      {showUserFeedback && (
                        <details className="mt-2">
                          <summary className="text-[10px] font-medium text-orange-600 dark:text-orange-400 cursor-pointer select-none list-none flex items-center gap-1">
                            <span className="transition-transform duration-200 text-[8px] [details[open]>&]:rotate-90">▶</span>
                            User Feedback
                          </summary>
                          <div className="mt-1">
                            <p className="text-xs text-dim/80 whitespace-pre-wrap">{taskData.retry_context}</p>
                          </div>
                        </details>
                      )}
                    </div>
                  );
                }
                // Grouped tool entries (tool_use + tool_activity) — handled before role checks
                if (msg.kind === "tool_use" || msg.kind === "tool_activity") {
                  if (toolGroups.has(msg.id)) {
                    const entries = toolGroups.get(msg.id);
                    if (entries.length > 0) {
                      return <div key={msg.id} data-msg-id={msg.id} data-msg-type="tool_group"><ToolLogBubble entries={entries} /></div>;
                    }
                  }
                  return null; // part of group or start-only leader with no entries
                }
                if (msg.role === "AGENT" && !(msg.content || "").trimStart().startsWith("<task-notification>")) {
                  // Case 2: text kind — render as simple ChatBubble
                  if (msg.kind === "text") {
                    return <div key={msg.id} data-msg-id={msg.id} data-msg-type="agent_text"><ChatBubble message={msg} project={agent.project} onCancelMessage={handleCancelMessage} onUpdateMessage={handleUpdateMessage} onSendNow={handleSendNow} agentId={id} onRefresh={refreshMessages} toolEntries={toolEntriesForText.get(msg.id)} openMenuMsgId={openMenuMsgId} setOpenMenuMsgId={setOpenMenuMsgId} bookmarkedSet={bookmarkedSet} onAfterBookmark={handleAfterBookmark} /></div>;
                  }
                  // Case 3: null/undefined kind (legacy) — existing splitMessageSegments logic
                  const segments = splitMessageSegments(msg.content || "");
                  if (segments.length > 1 || segments[0]?.type === "tools") {
                    const lastTextIdx = segments.findLastIndex((s) => s.type === "text");
                    return (
                      <div key={msg.id} data-msg-id={msg.id} data-msg-type="legacy_split">
                        {segments.map((seg, i) => {
                          if (seg.type === "tools") return <ToolLogBubble key={`${msg.id}-t${i}`} entries={seg.entries} />;
                          if (i === lastTextIdx) return <ChatBubble key={`${msg.id}-c`} message={msg} contentOverride={seg.text} project={agent.project} onCancelMessage={handleCancelMessage} onUpdateMessage={handleUpdateMessage} onSendNow={handleSendNow} agentId={id} onRefresh={refreshMessages} openMenuMsgId={openMenuMsgId} setOpenMenuMsgId={setOpenMenuMsgId} bookmarkedSet={bookmarkedSet} onAfterBookmark={handleAfterBookmark} />;
                          return <AgentTextSegment key={`${msg.id}-s${i}`} text={seg.text} project={agent.project} />;
                        })}
                        {lastTextIdx === -1 && msg.metadata?.interactive?.length > 0 && (
                          <ChatBubble key={`${msg.id}-c`} message={msg} contentOverride="" project={agent.project} onCancelMessage={handleCancelMessage} onUpdateMessage={handleUpdateMessage} onSendNow={handleSendNow} agentId={id} onRefresh={refreshMessages} openMenuMsgId={openMenuMsgId} setOpenMenuMsgId={setOpenMenuMsgId} bookmarkedSet={bookmarkedSet} onAfterBookmark={handleAfterBookmark} />
                        )}
                      </div>
                    );
                  }
                }
                return <div key={msg.id} data-msg-id={msg.id} data-msg-type={msg.role === "USER" ? "user" : msg.role === "SYSTEM" ? "system" : "agent_default"}><ChatBubble message={msg} project={agent.project} onCancelMessage={handleCancelMessage} onUpdateMessage={handleUpdateMessage} onSendNow={handleSendNow} agentId={id} onRefresh={refreshMessages} openMenuMsgId={openMenuMsgId} setOpenMenuMsgId={setOpenMenuMsgId} bookmarkedSet={bookmarkedSet} onAfterBookmark={handleAfterBookmark} /></div>;
              });
            })()}

            {/* Typing indicator while executing */}
            {isExecuting ? <div data-msg-id="typing" data-msg-type="typing_indicator"><TypingIndicator /></div> : null}

            {/* Failed / queued / scheduled messages at the bottom */}
            {(() => {
              const queued = queuedMessages;
              const scheduled = preSentMessages.filter((m) => m.role === "USER" && m.status === "scheduled" && m.scheduled_at);
              return (
                <>
                  {failedMessages.map((msg) => (
                    <div key={msg.id} data-msg-id={msg.id} data-msg-type="failed_msg" className="flex justify-end my-2">
                      <div className="max-w-[min(85%,30rem)] min-w-0 flex flex-col items-end">
                        <div className="flex items-center gap-2 max-w-full">
                          <div className="flex-shrink-0 text-red-400" title="Message not delivered">
                            <svg className="w-5 h-5" fill="none" stroke="currentColor" strokeWidth={2} viewBox="0 0 24 24">
                              <path strokeLinecap="round" strokeLinejoin="round" d="M12 9v4m0 4h.01M12 2L2 20h20L12 2z" />
                            </svg>
                          </div>
                          <div className="rounded-2xl px-4 py-2.5 bg-red-600/40 text-white/70 rounded-br-md overflow-hidden">
                            <div className="text-sm break-words chat-bubble-content">
                              <SafeMarkdown fallback={msg.content}>{renderMarkdown(msg.content, agent?.project)}</SafeMarkdown>
                            </div>
                            <div className="text-xs mt-1 flex items-center gap-1.5 text-red-300">
                              {relativeTime(msg.created_at)}
                              <span className="text-red-400">undelivered</span>
                              <span className="ml-auto text-red-400" title="Undelivered">
                                <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" strokeWidth={2.5} viewBox="0 0 24 24">
                                  <path strokeLinecap="round" strokeLinejoin="round" d="M6 18L18 6M6 6l12 12" />
                                </svg>
                              </span>
                            </div>
                          </div>
                        </div>
                        <div className="flex gap-2 mt-1 mr-1">
                          <button
                            onClick={() => handleRetryFailed(msg.id)}
                            className="text-xs text-accent hover:opacity-80 transition-colors"
                          >Retry</button>
                          <button
                            onClick={() => handleDismissFailed(msg.id)}
                            className="text-xs text-dim hover:text-body transition-colors"
                          >Dismiss</button>
                        </div>
                      </div>
                    </div>
                  ))}
                  {queued.map((msg, idx) => (
                    <div key={msg.id} data-msg-id={msg.id} data-msg-type="queued_msg"><ChatBubble message={msg} project={agent.project} onCancelMessage={handleCancelMessage} onUpdateMessage={handleUpdateMessage} onSendNow={handleSendNow} onStartEdit={handleStartEditMessage} editingMessageId={editingMessage?.id} agentId={id} onRefresh={refreshMessages} queuePosition={idx + 1} queueTotal={queued.length} openMenuMsgId={openMenuMsgId} setOpenMenuMsgId={setOpenMenuMsgId} bookmarkedSet={bookmarkedSet} onAfterBookmark={handleAfterBookmark} /></div>
                  ))}
                  {scheduled.map((msg) => (
                    <div key={msg.id} data-msg-id={msg.id} data-msg-type="scheduled_msg"><ChatBubble message={msg} project={agent.project} onCancelMessage={handleCancelMessage} onUpdateMessage={handleUpdateMessage} onSendNow={handleSendNow} onStartEdit={handleStartEditMessage} editingMessageId={editingMessage?.id} agentId={id} onRefresh={refreshMessages} openMenuMsgId={openMenuMsgId} setOpenMenuMsgId={setOpenMenuMsgId} bookmarkedSet={bookmarkedSet} onAfterBookmark={handleAfterBookmark} /></div>
                  ))}
                </>
              );
            })()}
          </>
        )}


        {/* Progress suggestions card */}
        {/* Insights bubble — single source of truth is the agent row in DB:
              insight_status=generating  → blue "Generating..." pill
              insight_status=failed      → red "Failed, retry" pill
              has_pending_suggestions    → amber pending-review card
              else                       → amber collapsed history ("X applied")
            Apply/Discard does NOT mutate frontend-only flags; the WS
            agent_update flips has_pending_suggestions=false and the parent
            naturally swaps ProgressSuggestionsCard for InsightsHistoryCard,
            which renders the same shape on refresh / re-entry. */}
        {agent?.status === "STOPPED" && agent?.insight_status === "generating" && (
          <InsightStatusCard status="generating" agentId={id} onRetry={loadData} />
        )}
        {agent?.status === "STOPPED" && agent?.insight_status === "failed" && (
          <InsightStatusCard status="failed" agentId={id} onRetry={loadData} />
        )}
        {criticalLoadDone && agent?.status === "STOPPED" && !agent?.insight_status && (
          agent?.has_pending_suggestions
            ? <ProgressSuggestionsCard
                agentId={id}
                onApplied={(n) => showToast(`${n} insight${n !== 1 ? "s" : ""} applied to PROGRESS.md`)}
                onDiscarded={() => showToast("Suggestions discarded")}
              />
            : <InsightsHistoryCard agentId={id} />
        )}

        <div ref={messagesEndRef} />
      </div>

      {/* Input bar */}
      <ChatInput
        agentId={id}
        project={agent?.project}
        onSend={handleSend}
        onSendLater={handleSendLater}
        editingMessage={editingMessage}
        onEditSave={handleSaveMessageEdit}
        onEditCancel={handleCancelMessageEdit}
        disabled={isStarting || isStopped || isError}
        disabledReason={disabledReason}
        isBusy={!hasTmux && isExecuting}
        tmuxMode={hasTmux}
        onEscape={hasTmuxPane ? async () => {
          try {
            await escapeAgent(id);
            // Also cancel any active queued messages so they don't
            // auto-dispatch after the interrupt — common when the user is
            // bailing out of a wedged TUI command (e.g. /plugin) with
            // backlog piled up. Soft-cancel: bubbles stay visible (greyed)
            // via WS message_update push.
            if (activeQueuedMessages.length > 0) {
              await Promise.allSettled(
                activeQueuedMessages.map((m) => cancelMessage(id, m.id)),
              );
            }
            // Kick the sync loop so freshly-written JSONL entries
            // (e.g. "Request interrupted by user", "/loop resuming"
            // system markers) get imported right away. Short delay
            // gives the sync loop a tick to write to DB before we
            // re-read it — same pattern as the manual refresh button.
            wakeSync(id).catch(() => {});
            setTimeout(() => loadData(), JSONL_FLUSH_DELAY_MS);
          } catch (e) { showToast(e.message || "Escape failed", "error"); }
        } : null}
        escapeDisabled={isStopped || isError}
        escapeUrgent={isExecuting || hasPendingInteractive || activeQueuedMessages.length > 0}
        escapeAvailable={hasTmuxPane}
        scrollButton={showScrollToBottom ? (
          <button
            type="button"
            onPointerDown={(e) => e.preventDefault()}
            onClick={() => {
              scrollToLiveTail();
              setShowScrollToBottom(false);
            }}
            className="glass-bar-nav w-9 h-9 rounded-full flex items-center justify-center text-dim hover:text-heading transition-all active:scale-90 pointer-events-auto mb-2"
          >
            <svg className="w-4.5 h-4.5" fill="none" stroke="currentColor" strokeWidth={2} viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" d="M19 14l-7 7m0 0l-7-7m7 7V3" />
            </svg>
          </button>
        ) : null}
        kbOpen={kbOpen}
      />

      {/* Stop confirmation modal */}
      {showStopConfirm && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4">
          <div className="bg-surface rounded-2xl p-6 max-w-sm w-full space-y-4 shadow-card">
            <h3 className="text-lg font-bold text-heading">Stop Agent?</h3>
            <p className="text-sm text-label">
              This will stop the agent. You won't be able to send more messages.
            </p>
            {agent?.task_id && (
              <>
                {/* Task outcome toggle */}
                <div className="flex gap-2">
                  <button
                    type="button"
                    onClick={() => setTaskOutcome("complete")}
                    className={`flex-1 px-3 py-2 rounded-lg text-sm font-medium transition-colors ${
                      taskOutcome === "complete"
                        ? "bg-green-600 text-white"
                        : "bg-input text-body hover:bg-elevated"
                    }`}
                  >
                    Done
                  </button>
                  <button
                    type="button"
                    onClick={() => setTaskOutcome("incomplete")}
                    className={`flex-1 px-3 py-2 rounded-lg text-sm font-medium transition-colors ${
                      taskOutcome === "incomplete"
                        ? "bg-amber-600 text-white"
                        : "bg-input text-body hover:bg-elevated"
                    }`}
                  >
                    Redo
                  </button>
                  <button
                    type="button"
                    onClick={() => setTaskOutcome("drop")}
                    className={`flex-1 px-3 py-2 rounded-lg text-sm font-medium transition-colors ${
                      taskOutcome === "drop"
                        ? "bg-gray-600 text-white"
                        : "bg-input text-body hover:bg-elevated"
                    }`}
                  >
                    Drop
                  </button>
                </div>
                {/* Feedback note for Redo / Drop */}
                {taskOutcome !== "complete" && (
                  <div className="space-y-2">
                    <textarea
                      value={incompleteReason}
                      onChange={(e) => setIncompleteReason(e.target.value)}
                      onPaste={handleFeedbackPaste}
                      placeholder="What should be done differently? (optional)"
                      rows={3}
                      className="w-full px-3 py-2 rounded-lg bg-input border border-divider text-body text-sm placeholder:text-dim resize-none focus:outline-none focus:ring-1 focus:ring-amber-500/50"
                    />
                    {/* Feedback attachment previews */}
                    {feedbackAttachments.length > 0 && (
                      <div className="flex flex-wrap gap-1.5">
                        {feedbackAttachments.map((att) => (
                          <div key={att.id} className="flex items-center gap-1 px-2 py-1 rounded-lg bg-elevated text-xs max-w-[140px]">
                            {att.previewUrl ? (
                              <img src={att.previewUrl} alt="" className="w-8 h-8 rounded object-cover shrink-0" />
                            ) : (
                              <svg className="w-4 h-4 text-dim shrink-0" fill="none" stroke="currentColor" strokeWidth={1.5} viewBox="0 0 24 24">
                                <path strokeLinecap="round" strokeLinejoin="round" d="M19.5 14.25v-2.625a3.375 3.375 0 00-3.375-3.375h-1.5A1.125 1.125 0 0113.5 7.125v-1.5a3.375 3.375 0 00-3.375-3.375H8.25m2.25 0H5.625c-.621 0-1.125.504-1.125 1.125v17.25c0 .621.504 1.125 1.125 1.125h12.75c.621 0 1.125-.504 1.125-1.125V11.25a9 9 0 00-9-9z" />
                              </svg>
                            )}
                            <span className="truncate text-label flex-1 min-w-0">{att.originalName}</span>
                            {att.uploading ? (
                              <svg className="w-3.5 h-3.5 text-amber-400 animate-spin shrink-0" fill="none" viewBox="0 0 24 24">
                                <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
                                <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z" />
                              </svg>
                            ) : (
                              <button type="button" onClick={() => removeFeedbackAttachment(att.id)} className="text-dim hover:text-heading shrink-0">
                                <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" strokeWidth={2} viewBox="0 0 24 24">
                                  <path strokeLinecap="round" strokeLinejoin="round" d="M6 18L18 6M6 6l12 12" />
                                </svg>
                              </button>
                            )}
                          </div>
                        ))}
                      </div>
                    )}
                    {/* Toolbar: attach + voice */}
                    <div className="flex items-center gap-1.5">
                      <button
                        type="button"
                        onClick={() => feedbackFileInputRef.current?.click()}
                        title="Attach files"
                        className="w-8 h-8 rounded-full flex items-center justify-center bg-elevated hover:bg-hover text-label transition-colors"
                      >
                        <svg className="w-4 h-4" fill="none" stroke="currentColor" strokeWidth={2} viewBox="0 0 24 24">
                          <path strokeLinecap="round" strokeLinejoin="round" d="M12 4v16m8-8H4" />
                        </svg>
                      </button>
                      <input ref={feedbackFileInputRef} type="file" accept="image/*,video/*,.pdf,.txt,.csv,.json,.md,.py,.js,.ts,.jsx,.tsx,.html,.css,.yaml,.yml,.xml,.log,.zip,.tar,.gz" multiple className="hidden" onChange={(e) => { const files = Array.from(e.target.files || []); e.target.value = ""; if (files.length > 0) addFeedbackFiles(files); }} />
                      <div className="flex-1" />
                      {feedbackVoice.recording && feedbackVoice.remainingSeconds != null && (
                        <span className={`text-xs font-semibold tabular-nums ${feedbackVoice.remainingSeconds <= 10 ? "text-red-400" : "text-red-500"}`}>
                          {feedbackVoice.remainingSeconds >= 60
                            ? `${Math.floor(feedbackVoice.remainingSeconds / 60)}:${String(feedbackVoice.remainingSeconds % 60).padStart(2, "0")}`
                            : feedbackVoice.remainingSeconds}
                        </span>
                      )}
                      <VoiceRecorder
                        recording={feedbackVoice.recording}
                        voiceLoading={feedbackVoice.voiceLoading}
                        refining={feedbackVoice.refining}
                        micError={feedbackVoice.micError}
                        onToggle={feedbackVoice.toggleRecording}
                      />
                    </div>
                    {feedbackVoice.streamingText && (
                      <div className="px-1 text-xs text-amber-400/80 italic animate-pulse">{feedbackVoice.streamingText}</div>
                    )}
                  </div>
                )}
                {/* Generate summary checkbox */}
                <label className="flex items-center gap-2 cursor-pointer select-none">
                  <input
                    type="checkbox"
                    checked={generateSummary}
                    onChange={(e) => setGenerateSummary(e.target.checked)}
                    className="w-4 h-4 rounded border-divider bg-input text-amber-500 focus:ring-amber-500/50"
                  />
                  <span className="text-sm text-body">Generate progress summary</span>
                </label>
              </>
            )}
            <div className="flex gap-3">
              <button
                type="button"
                disabled={stopping}
                onClick={handleStop}
                className="flex-1 min-h-[44px] rounded-lg bg-red-600 hover:bg-red-500 text-white font-semibold text-sm transition-colors disabled:opacity-50"
              >
                {stopping ? "Stopping..." : "Stop"}
              </button>
              <button
                type="button"
                onClick={() => { setShowStopConfirm(false); setTaskOutcome("complete"); setIncompleteReason(""); clearFeedbackAttachments(); }}
                className="flex-1 min-h-[44px] rounded-lg bg-input hover:bg-elevated text-body text-sm transition-colors"
              >
                Cancel
              </button>
            </div>
          </div>
        </div>
      )}

      {fileModal && agent && (
        <ProjectFileModal
          project={agent.project}
          filename={fileModal}
          onClose={() => {
            setFileModal(null);
            Promise.all([
              fetchProjectFile(agent.project, "CLAUDE.md").catch(() => ({ exists: false })),
              fetchProjectFile(agent.project, "PROGRESS.md").catch(() => ({ exists: false })),
            ]).then(([c, p]) => {
              setFileExists({ "CLAUDE.md": c.exists, "PROGRESS.md": p.exists });
            });
          }}
        />
      )}

      {showTerminal && (
        <Suspense fallback={null}>
          <TerminalOverlay
            agentId={id}
            agentName={agent?.name}
            onClose={() => setShowTerminal(false)}
          />
        </Suspense>
      )}
      {showBrowser && agent && (
        <ProjectBrowserModal
          project={agent.project}
          agentId={id}
          onClose={() => setShowBrowser(false)}
        />
      )}

      {showTaskCard && agent?.task_id && (
        <FloatingTaskCard
          taskId={agent.task_id}
          onClose={() => setShowTaskCard(false)}
          onAction={(action) => {
            setShowTaskCard(false);
            if (action === "complete") {
              setTaskOutcome("complete");
              setShowStopConfirm(true);
            } else if (action === "incomplete") {
              setTaskOutcome("incomplete");
              setShowStopConfirm(true);
            }
          }}
        />
      )}
      <BookmarkNotePrompt
        project={agent?.project}
        messageId={noteForBookmarkId}
        onClose={() => setNoteForBookmarkId(null)}
      />
    </div>
  );
}
