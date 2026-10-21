import { useState, useEffect, useLayoutEffect, useRef, useMemo } from "react";
import { useNavigate, useLocation } from "react-router-dom";
import { createTaskV2, dispatchTask, uploadFile, generateWorktreeName } from "../lib/api";
import { MODEL_OPTIONS, DEFAULT_MODEL, modelDisplayName } from "../lib/constants";
import { DATE_SHORT } from "../lib/formatters";
import TagPicker from "../components/cards/TagPicker";
import useProjects from "../hooks/useProjects";
import SendLaterPicker from "../components/SendLaterPicker";
import ImageLightbox from "../components/ImageLightbox";
import useDraft from "../hooks/useDraft";
import useVoiceRecorder from "../hooks/useVoiceRecorder";
import { useToast } from "../contexts/ToastContext";
import { attachmentUrl } from "../lib/urls";
import { draftAttachmentForCache, restoreDraftAttachment } from "../lib/draftAttachments";

function deriveTitle(description) {
  if (!description) return "";
  const text = description.trim();
  if (text.length <= 60) return text;
  const cut = text.slice(0, 60);
  const spaceIdx = cut.lastIndexOf(" ");
  return (spaceIdx > 20 ? cut.slice(0, spaceIdx) : cut) + "...";
}

const MODEL_PICKER = MODEL_OPTIONS.map(m => ({ value: m.value, label: m.label }));
const EFFORT_PICKER = [
  { value: "low", label: "Low" },
  { value: "medium", label: "Medium" },
  { value: "high", label: "High" },
  { value: "xhigh", label: "XHigh" },
  { value: "max", label: "Max" },
  // xhigh + standing multi-agent workflow orchestration (claude --effort
  // ultracode). Token-heavy — opt-in per task, never the default.
  { value: "ultracode", label: "Ultracode" },
];
const WT_PICKER = [
  { value: true, label: "On" },
  { value: false, label: "Off" },
];
const AUTO_PICKER = [
  { value: true, label: "On" },
  { value: false, label: "Off" },
];

export default function NewTaskPage({ embedded = false, onClose, contextPath }) {
  const navigate = useNavigate();
  const location = useLocation();
  const hasBackground = !!onClose || !!location.state?.backgroundLocation;
  // useTmux removed — all tasks use tmux now
  const [description, setDescription, clearDesc] = useDraft("new-task:description", "");
  const [project, setProject, clearProject] = useDraft("new-task:project", "");
  const [model, setModel, clearModel] = useDraft("new-task:model", DEFAULT_MODEL);
  const [effort, setEffort, clearEffort] = useDraft("new-task:effort", "xhigh");
  const [priority, setPriority] = useState(0);
  const [skipPermissions, setSkipPermissions] = useState(() => {
    try { return localStorage.getItem("pref:skipPermissions") !== "false"; } catch { return true; }
  });
  const [worktree, setWorktree] = useState(() => {
    try { const v = localStorage.getItem("pref:worktree"); return v !== null ? (v === "" ? null : v) : "auto"; } catch { return "auto"; }
  });
  const [syncMode, setSyncMode] = useState(() => {
    try { return localStorage.getItem("pref:syncMode") === "true"; } catch { return false; }
  });
  const [submitting, setSubmitting] = useState(false);
  const [showSchedulePicker, setShowSchedulePicker] = useState(false);
  const [notifyAt, setNotifyAt] = useState(null);
  const [projectFlash, setProjectFlash] = useState(0);
  const textareaRef = useRef(null);
  const fileInputRef = useRef(null);
  const sheetBodyRef = useRef(null);
  const containerRef = useRef(null);

  // Compose-bar animation state
  const [mounted, setMounted] = useState(false);
  const [isClosing, setIsClosing] = useState(false);
  const [kbOpen, setKbOpen] = useState(false);
  const sheetRef = useRef(null);

  // Initial position: off-screen (runs before first paint)
  useLayoutEffect(() => {
    const el = sheetRef.current;
    if (el) el.style.transform = 'translateY(120%)';
  }, []);

  // Prevent background scroll. overflow:hidden handles normal scroll;
  // scroll listener resets any programmatic scroll iOS does on keyboard open.
  // Skip in embedded (split-screen) mode — global body changes cause layout
  // reflow across all panes.
  useLayoutEffect(() => {
    if (embedded) return;
    const scrollY = window.scrollY;
    document.documentElement.style.overflow = 'hidden';
    document.body.style.overflow = 'hidden';
    document.documentElement.style.backgroundColor = '#999';
    document.body.style.backgroundColor = '#999';
    const resetScroll = () => window.scrollTo(0, scrollY);
    window.addEventListener('scroll', resetScroll);
    return () => {
      window.removeEventListener('scroll', resetScroll);
      document.documentElement.style.overflow = '';
      document.body.style.overflow = '';
      document.documentElement.style.backgroundColor = '';
      document.body.style.backgroundColor = '';
    };
  }, [embedded]);

  // Track keyboard height via visualViewport — sets CSS var --kb-h for
  // positioning the sheet above the keyboard.
  useEffect(() => {
    const vv = window.visualViewport;
    if (!vv) return;
    let rafId = null;
    let stopTimer = null;
    let prevOff = 0;
    let wasOpen = false;

    const update = () => {
      const el = containerRef.current;
      if (!el) return;

      const containerH = el.clientHeight;
      const rawDelta = Math.max(0, Math.round(containerH - vv.height));
      const kbOffset = Math.max(0, Math.round(containerH - vv.height - vv.offsetTop));

      const open = rawDelta > 100;

      if (open) {
        if (Math.abs(kbOffset - prevOff) > 3) {
          prevOff = kbOffset;
          el.style.setProperty('--kb-h', `${kbOffset}px`);
        }
      } else if (prevOff !== 0) {
        prevOff = 0;
        el.style.removeProperty('--kb-h');
      }

      if (open !== wasOpen) {
        wasOpen = open;
        setKbOpen(open);
      }
    };

    const poll = () => { update(); rafId = requestAnimationFrame(poll); };
    const startPoll = () => {
      if (stopTimer) { clearTimeout(stopTimer); stopTimer = null; }
      if (!rafId) rafId = requestAnimationFrame(poll);
    };
    const stopPoll = () => {
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
    };
  }, []);

  // When opened from a project detail page, default the project field to
  // that project — overrides the persisted draft so the sheet reflects the
  // user's current context.
  useEffect(() => {
    const bgPath = contextPath || location.state?.backgroundLocation?.pathname;
    if (!bgPath) return;
    const m = bgPath.match(/^\/projects\/([^/]+)/);
    if (m) {
      try { setProject(decodeURIComponent(m[1])); } catch { setProject(m[1]); }
    }
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  // Slide sheet up + fade in backdrop
  useEffect(() => {
    requestAnimationFrame(() => requestAnimationFrame(() => {
      setMounted(true);
      const el = sheetRef.current;
      if (el) {
        el.style.transition = 'transform 0.3s cubic-bezier(0.32, 0.72, 0, 1)';
        el.style.transform = 'translateY(0px)';
      }
    }));
  }, []);

  // Block scroll on non-body areas within the overlay (native listener —
  // React 18 registers touchmove as passive, so preventDefault is a no-op).
  useEffect(() => {
    const el = containerRef.current;
    if (!el) return;
    const block = (e) => {
      if (sheetBodyRef.current?.contains(e.target)) return;
      e.preventDefault();
    };
    el.addEventListener("touchmove", block, { passive: false });
    return () => el.removeEventListener("touchmove", block);
  }, []);

  // Block touchmove on ANY element outside all overlays — catches
  // iOS Safari momentum-scroll bleed from the page behind.
  // Skip in embedded mode — other panes need their scroll events.
  useEffect(() => {
    if (embedded) return;
    const blockBg = (e) => {
      if (e.target.closest('[data-overlay]')) return;
      e.preventDefault();
    };
    document.addEventListener("touchmove", blockBg, { passive: false });
    return () => document.removeEventListener("touchmove", blockBg);
  }, [embedded]);

  const [previewIndex, setPreviewIndex] = useState(null);

  // Attachments
  const attachmentCacheKey = "draft:new-task:attachments";
  const [attachments, setAttachments] = useState(() => {
    try {
      const cached = localStorage.getItem(attachmentCacheKey);
      if (cached) {
        return JSON.parse(cached).map((a) => restoreDraftAttachment(a, attachmentUrl));
      }
    } catch { /* corrupt cache */ }
    return [];
  });
  const [dragOver, setDragOver] = useState(false);
  const dragCountRef = useRef(0);

  const clearAllDrafts = () => { clearDesc(); };

  const toast = useToast();
  const showToast = (message, type = "success") => type === "error" ? toast.error(message) : toast.success(message);
  const { projects } = useProjects();
  const projectPicker = useMemo(() => [
    { value: "", label: "None" },
    ...projects.map(p => ({ value: p.name, label: p.display_name || p.name })),
  ], [projects]);

  const voice = useVoiceRecorder({
    onTranscript: (text) => setDescription((prev) => (prev ? prev + " " + text : text)),
    onError: (msg) => showToast(msg, "error"),
    persistKey: "voice:new-task",
  });

  // Auto-resize textarea
  useEffect(() => {
    const el = textareaRef.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = Math.min(el.scrollHeight, 200) + "px";
  }, [description]);

  // Cleanup blob URLs on unmount
  useEffect(() => {
    return () => { attachments.forEach((a) => { if (a.previewUrl?.startsWith("blob:")) URL.revokeObjectURL(a.previewUrl); }); };
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  // Sync completed attachments to localStorage
  useEffect(() => {
    const completed = attachments.filter((a) => !a.uploading && a.uploadedPath);
    if (completed.length > 0) {
      // Path + metadata only: the preview URL (media token) is rebuilt on restore.
      const toCache = completed.map(draftAttachmentForCache);
      try { localStorage.setItem(attachmentCacheKey, JSON.stringify(toCache)); } catch { /* quota */ }
    } else {
      try { localStorage.removeItem(attachmentCacheKey); } catch { /* unavailable */ }
    }
  }, [attachments]);

  const addFiles = (files) => {
    for (const file of files) {
      const id = Math.random().toString(36).slice(2, 10);
      const isImage = file.type.startsWith("image/");
      const previewUrl = isImage ? URL.createObjectURL(file) : null;
      setAttachments((prev) => [...prev, { id, file, previewUrl, uploading: true, uploadedPath: null, originalName: file.name, size: file.size, mimeType: file.type }]);
      uploadFile(file, project || null).then((result) => {
        setAttachments((prev) => prev.map((a) => a.id === id ? { ...a, uploading: false, uploadedPath: result.path } : a));
      }).catch((err) => {
        setAttachments((prev) => prev.filter((a) => a.id !== id));
        if (previewUrl) URL.revokeObjectURL(previewUrl);
        showToast(`Upload failed: ${err.message}`, "error");
      });
    }
  };

  const removeAttachment = (id) => {
    setAttachments((prev) => {
      const att = prev.find((a) => a.id === id);
      if (att?.previewUrl?.startsWith("blob:")) URL.revokeObjectURL(att.previewUrl);
      return prev.filter((a) => a.id !== id);
    });
  };

  const clearAttachments = () => {
    setAttachments((prev) => { prev.forEach((a) => { if (a.previewUrl?.startsWith("blob:")) URL.revokeObjectURL(a.previewUrl); }); return []; });
    try { localStorage.removeItem(attachmentCacheKey); } catch { /* unavailable */ }
  };

  const handleDragEnter = (e) => { e.preventDefault(); e.stopPropagation(); dragCountRef.current++; if (e.dataTransfer?.types?.includes("Files")) setDragOver(true); };
  const handleDragLeave = (e) => { e.preventDefault(); e.stopPropagation(); dragCountRef.current--; if (dragCountRef.current <= 0) { dragCountRef.current = 0; setDragOver(false); } };
  const handleDragOver = (e) => { e.preventDefault(); e.stopPropagation(); };
  const handleDrop = (e) => { e.preventDefault(); e.stopPropagation(); dragCountRef.current = 0; setDragOver(false); const files = Array.from(e.dataTransfer?.files || []); if (files.length > 0) addFiles(files); };
  const handlePaste = (e) => {
    const items = e.clipboardData?.items;
    if (!items) return;
    const files = [];
    for (const item of items) { if (item.kind === "file") { const f = item.getAsFile(); if (f) files.push(f); } }
    if (files.length > 0) { e.preventDefault(); addFiles(files); }
  };
  const handleFileSelect = (e) => { const files = Array.from(e.target.files || []); e.target.value = ""; if (files.length > 0) addFiles(files); };

  const anyUploading = attachments.some((a) => a.uploading);

  const buildDescriptionText = (baseText, atts) => {
    let msg = baseText;
    for (const a of atts) { if (a.uploadedPath) msg += `\n[Attached file: ${a.uploadedPath}]`; }
    return msg;
  };

  // ---- Dismiss (swipe down / backdrop tap) → save to inbox ----
  const dismissClosingRef = useRef(false);
  const submittingRef = useRef(false);
  const dismiss = async () => {
    if (dismissClosingRef.current || submittingRef.current) return;
    dismissClosingRef.current = true;
    if (hasContent) {
      try {
        const uploaded = attachments.filter((a) => a.uploadedPath);
        const fullDescription = buildDescriptionText(description.trim(), uploaded);
        let finalTitle = deriveTitle(description);
        if (!finalTitle && uploaded.length > 0) finalTitle = "Untitled task";
        await createTaskV2({
          title: finalTitle,
          description: fullDescription || undefined,
          project_name: project || undefined,
          priority,
          model: model || undefined,
          effort: effort || undefined,
          skip_permissions: skipPermissions,
          sync_mode: false,
          use_worktree: !!worktree,
          use_tmux: true,
          notify_at: notifyAt || undefined,
          auto_dispatch: false, // inbox only
        });
        clearAllDrafts();
        clearAttachments();
        showToast("Saved to inbox");
      } catch (err) {
        showToast("Failed to save: " + err.message, "error");
        dismissClosingRef.current = false;
        return;
      }
    }
    setIsClosing(true);
    const el = sheetRef.current;
    if (el) {
      el.style.transition = 'transform 0.3s cubic-bezier(0.32, 0.72, 0, 1)';
      el.style.transform = 'translateY(100%)';
    }
    setTimeout(() => onClose ? onClose() : (hasBackground ? navigate(-1) : navigate("/tasks", { replace: true })), 250);
  };

  // ---- Submit (enter key) → save to inbox ----
  const handleSubmit = async (e) => {
    if (e) e.preventDefault();
    await dismiss();
  };

  // ---- Quick save: store to inbox, clear input, keep settings ----
  const quickSave = async () => {
    if (submittingRef.current) return;
    const hasText = description.trim() || attachments.some((a) => a.uploadedPath);
    if (!hasText || attachments.some((a) => a.uploading)) return;
    submittingRef.current = true;
    setSubmitting(true);
    try {
      const uploaded = attachments.filter((a) => a.uploadedPath);
      const fullDescription = buildDescriptionText(description.trim(), uploaded);
      let finalTitle = deriveTitle(description);
      if (!finalTitle && uploaded.length > 0) finalTitle = "Untitled task";
      await createTaskV2({
        title: finalTitle,
        description: fullDescription || undefined,
        project_name: project || undefined,
        priority,
        model: model || undefined,
        effort: effort || undefined,
        skip_permissions: skipPermissions,
        sync_mode: false,
        use_worktree: !!worktree,
        use_tmux: true,
        notify_at: notifyAt || undefined,
        auto_dispatch: false,
      });
      setDescription("");
      clearAttachments();
      setNotifyAt(null);
      showToast("Saved to inbox");
      textareaRef.current?.focus();
    } catch (err) {
      showToast("Failed to save: " + err.message, "error");
    } finally {
      submittingRef.current = false;
      setSubmitting(false);
    }
  };

  // ---- Launch agent (when project is selected) ----
  // Optimistic: dismiss sheet immediately, run create+dispatch in background.
  const launchAgent = () => {
    if (submittingRef.current) return;
    if (!project) {
      setProjectFlash((n) => n + 1);
      return;
    }
    const hasText = description.trim() || attachments.some((a) => a.uploadedPath);
    if (!hasText || attachments.some((a) => a.uploading)) return;
    submittingRef.current = true;
    setSubmitting(true);

    const uploaded = attachments.filter((a) => a.uploadedPath);
    const fullPrompt = buildDescriptionText(description.trim(), uploaded);
    let finalTitle = deriveTitle(description);
    if (!finalTitle && uploaded.length > 0) finalTitle = "Untitled task";

    // Snapshot payload, then clear drafts/UI synchronously.
    const payload = {
      title: finalTitle,
      description: fullPrompt || undefined,
      project_name: project,
      priority,
      model: model || undefined,
      effort: effort || undefined,
      skip_permissions: skipPermissions,
      use_worktree: !!worktree,
      use_tmux: true,
      auto_dispatch: false,
    };
    clearAllDrafts();
    clearAttachments();
    setNotifyAt(null);

    // Dismiss sheet immediately + return to previous page.
    setIsClosing(true);
    const el = sheetRef.current;
    if (el) {
      el.style.transition = 'transform 0.3s cubic-bezier(0.32, 0.72, 0, 1)';
      el.style.transform = 'translateY(100%)';
    }
    setTimeout(() => onClose ? onClose() : (hasBackground ? navigate(-1) : navigate("/tasks", { replace: true })), 250);

    // Background: create + dispatch. Toast on failure only.
    (async () => {
      try {
        const task = await createTaskV2(payload);
        await dispatchTask(task.id);
      } catch (err) {
        showToast("Launch failed: " + err.message, "error");
      } finally {
        submittingRef.current = false;
        setSubmitting(false);
      }
    })();
  };

  // ---- Attach/detach notify_at reminder time ----
  const handlePickReminder = (isoString) => {
    setNotifyAt(isoString);
    setShowSchedulePicker(false);
    showToast("Reminder attached");
  };

  const hasContent = description.trim() || attachments.some((a) => a.uploadedPath);
  const canSubmit = hasContent && !submitting && !anyUploading;

  return (
    <div
      ref={containerRef}
      data-overlay
      className={`${embedded ? "absolute" : "fixed"} inset-0 z-50 flex flex-col justify-end items-center`}
    >
      {/* Backdrop — pointer-events disabled until mounted to prevent stray
           click events synthesized from the FAB touch that opened this overlay */}
      <div
        className="absolute inset-0 transition-opacity duration-300"
        style={{ backgroundColor: "rgba(0,0,0,0.4)", opacity: mounted && !isClosing ? 1 : 0, touchAction: "none", pointerEvents: mounted && !isClosing ? "auto" : "none" }}
        onClick={() => dismiss()}
      />

      {/* Floating compose bar — single layer, no sheet wrapper.
           Sits at the bottom of the visual viewport (rides up with the
           on-screen keyboard via the bottom-offset effect below).
           transform/transition managed via sheetRef for slide-in/out. */}
      <div
        ref={sheetRef}
        data-popup-bounds
        className="relative z-10 mx-3 w-[calc(100%-1.5rem)] max-w-2xl bg-input shadow-card rounded-2xl"
        style={{
          maxHeight: "85vh",
          marginBottom: kbOpen ? 'var(--kb-h, 0px)' : 'max(12px, env(safe-area-inset-bottom, 0px))',
          transition: 'transform 0.3s cubic-bezier(0.32, 0.72, 0, 1)',
        }}
      >
        <div
          ref={sheetBodyRef}
          className="overflow-y-auto overflow-x-hidden px-5 pt-5 pb-3 rounded-2xl"
          style={{ overscrollBehavior: "none", maxHeight: "85vh" }}
        >
          <form
            onSubmit={handleSubmit}
            autoComplete="off"
            className="relative"
            onDragEnter={handleDragEnter}
            onDragLeave={handleDragLeave}
            onDragOver={handleDragOver}
            onDrop={handleDrop}
          >
            {dragOver && (
              <div className="absolute -inset-5 z-30 rounded-2xl accent-tint-15 border-2 border-dashed border-accent flex items-center justify-center pointer-events-none">
                <span className="text-sm font-medium text-accent">Drop files here</span>
              </div>
            )}
              <textarea
                ref={textareaRef}
                value={description}
                onChange={(e) => setDescription(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
                    e.preventDefault();
                    launchAgent();
                  }
                }}
                onPaste={handlePaste}
                placeholder="Describe what needs to be done..."
                rows={3}
                className="w-full min-h-[60px] max-h-[180px] bg-transparent text-sm text-heading placeholder-hint/40 resize-none focus:outline-none leading-relaxed"
              />
              {voice.refining && (
                <div className="text-sm text-accent-70 italic animate-pulse">
                  Refining...
                </div>
              )}
              {attachments.length > 0 && (
                <div className="flex flex-wrap gap-1.5 mt-2">
                  {attachments.map((att, i) => (
                    <div key={att.id} className="flex items-center gap-1 px-2 py-1 rounded-lg bg-elevated text-xs max-w-[160px] cursor-pointer"
                      onClick={() => { if (!att.uploading) setPreviewIndex(i); }}>
                      {att.previewUrl ? (
                        <img src={att.previewUrl} alt="" className="w-6 h-6 rounded object-cover shrink-0" />
                      ) : (
                        <svg className="w-3.5 h-3.5 text-dim shrink-0" fill="none" stroke="currentColor" strokeWidth={1.5} viewBox="0 0 24 24">
                          <path strokeLinecap="round" strokeLinejoin="round" d="M19.5 14.25v-2.625a3.375 3.375 0 00-3.375-3.375h-1.5A1.125 1.125 0 0113.5 7.125v-1.5a3.375 3.375 0 00-3.375-3.375H8.25m2.25 0H5.625c-.621 0-1.125.504-1.125 1.125v17.25c0 .621.504 1.125 1.125 1.125h12.75c.621 0 1.125-.504 1.125-1.125V11.25a9 9 0 00-9-9z" />
                        </svg>
                      )}
                      <span className="truncate flex-1 min-w-0 text-dim">{att.originalName}</span>
                      {att.uploading ? (
                        <svg className="w-3.5 h-3.5 text-accent animate-spin shrink-0" fill="none" viewBox="0 0 24 24">
                          <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
                          <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z" />
                        </svg>
                      ) : (
                        <button type="button" onClick={(e) => { e.stopPropagation(); removeAttachment(att.id); }} className="shrink-0 text-faint hover:text-heading">
                          <svg className="w-3 h-3" fill="none" stroke="currentColor" strokeWidth={2} viewBox="0 0 24 24">
                            <path strokeLinecap="round" strokeLinejoin="round" d="M6 18L18 6M6 6l12 12" />
                          </svg>
                        </button>
                      )}
                    </div>
                  ))}
                </div>
              )}
              <div className="flex flex-wrap items-center gap-1.5 mt-3" key={projectFlash}>
                <TagPicker options={projectPicker} value={project} onSelect={setProject} placement="top"
                  className={`text-[11px] font-medium rounded-full px-2 py-0.5 cursor-pointer active:scale-90 transition-transform ${
                    project
                      ? "accent-tint-15 text-accent"
                      : `bg-elevated text-faint ${projectFlash ? "bookmark-flash" : ""}`
                  }`}>
                  {project || "Project"}
                </TagPicker>
                <TagPicker options={WT_PICKER} value={!!worktree} placement="top"
                  keepOpenOnSelect={(v) => v === true}
                  onSelect={async (v) => {
                    if (!v) { setWorktree(null); try { localStorage.setItem("pref:worktree", ""); } catch {} return; }
                    setWorktree("...");
                    const name = description.trim() ? await generateWorktreeName(description).catch(() => null) : null;
                    const val = name || "auto";
                    setWorktree(val);
                    try { localStorage.setItem("pref:worktree", val); } catch {}
                  }}
                  className={`text-[11px] font-medium px-1.5 py-0.5 rounded-full cursor-pointer active:scale-90 transition-all ${
                    worktree ? "bg-purple-500/15 text-purple-500 dark:text-purple-400" : "bg-elevated text-faint"
                  }`}
                  extra={worktree ? (
                    <input
                      type="text"
                      name="worktree-name"
                      autoComplete="off"
                      placeholder={worktree === "..." ? "generating..." : "name (blank = auto)"}
                      value={worktree === "auto" || worktree === "..." ? "" : worktree}
                      onClick={(e) => e.stopPropagation()}
                      onChange={(e) => { e.stopPropagation(); const val = e.target.value || "auto"; setWorktree(val); try { localStorage.setItem("pref:worktree", val); } catch {} }}
                      className="w-full mt-1 px-2 py-1.5 rounded-lg text-xs bg-elevated text-heading placeholder-hint outline-none border border-edge/30 focus:border-accent transition-colors"
                    />
                  ) : null}
                >
                  <span className="flex items-center gap-0.5">
                    <svg className="w-3 h-3" fill="none" stroke="currentColor" strokeWidth={2} viewBox="0 0 24 24">
                      <path strokeLinecap="round" strokeLinejoin="round" d="M6 3v12M18 9a3 3 0 100-6 3 3 0 000 6zm0 0v3a3 3 0 01-3 3H9m-3 0a3 3 0 100 6 3 3 0 000-6z" />
                    </svg>
                    Worktree
                  </span>
                </TagPicker>
                <TagPicker options={AUTO_PICKER} value={skipPermissions} placement="top" onSelect={(v) => {
                  setSkipPermissions(v);
                  try { localStorage.setItem("pref:skipPermissions", String(v)); } catch {}
                }}
                  className={`text-[11px] font-medium px-1.5 py-0.5 rounded-full cursor-pointer active:scale-90 transition-all ${
                    skipPermissions ? "bg-amber-500/15 text-amber-500 dark:text-amber-400" : "bg-elevated text-faint"
                  }`}>
                  Auto
                </TagPicker>
                <TagPicker options={MODEL_PICKER} value={model} onSelect={setModel} placement="top"
                  className="text-[11px] font-medium px-2 py-0.5 rounded-full bg-elevated text-dim cursor-pointer active:scale-90 transition-transform">
                  {modelDisplayName(model)}
                </TagPicker>
                <TagPicker options={EFFORT_PICKER} value={effort} onSelect={setEffort} placement="top"
                  className="text-[11px] font-medium px-1.5 py-0.5 rounded-full bg-elevated text-dim cursor-pointer active:scale-90 transition-transform">
                  {EFFORT_PICKER.find(e => e.value === effort)?.label || effort}
                </TagPicker>
              </div>
              <div className="flex items-center gap-2 mt-3">
                <input ref={fileInputRef} type="file" accept="image/*,video/*,.pdf,.txt,.csv,.json,.md,.py,.js,.ts,.jsx,.tsx,.html,.css,.yaml,.yml,.xml,.log,.zip,.tar,.gz" multiple className="hidden" onChange={handleFileSelect} />
                <button
                  type="button"
                  onClick={() => fileInputRef.current?.click()}
                  title="Attach files"
                  className="w-10 h-10 rounded-full bg-elevated flex items-center justify-center text-dim hover:text-heading active:scale-90 transition-all"
                >
                  <svg className="w-5 h-5" fill="none" stroke="currentColor" strokeWidth={2} viewBox="0 0 24 24">
                    <path strokeLinecap="round" strokeLinejoin="round" d="M12 4v16m8-8H4" />
                  </svg>
                </button>
                <div className="flex-1" />
                {voice.recording && voice.remainingSeconds != null && (
                  <span className="text-[10px] text-red-400 font-semibold tabular-nums">
                    {Math.floor(voice.remainingSeconds / 60)}:{String(voice.remainingSeconds % 60).padStart(2, "0")}
                  </span>
                )}
                <button
                  type="button"
                  onClick={voice.toggleRecording}
                  disabled={voice.voiceLoading}
                  className={`w-10 h-10 rounded-full flex items-center justify-center transition-all active:scale-90 ${
                    voice.recording ? "bg-red-500 text-white"
                      : voice.voiceLoading ? "bg-elevated cursor-wait"
                      : "bg-elevated text-dim hover:text-heading"
                  }`}
                  title={voice.recording ? "Stop recording" : "Voice input"}
                >
                  {voice.voiceLoading ? (
                    <svg className="animate-spin w-5 h-5 text-body" viewBox="0 0 24 24" fill="none">
                      <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
                      <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z" />
                    </svg>
                  ) : (
                    <svg className="w-5 h-5" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                      <path d="M12 1a3 3 0 0 0-3 3v8a3 3 0 0 0 6 0V4a3 3 0 0 0-3-3z" />
                      <path d="M19 10v2a7 7 0 0 1-14 0v-2" />
                      <line x1="12" y1="19" x2="12" y2="23" />
                      <line x1="8" y1="23" x2="16" y2="23" />
                    </svg>
                  )}
                </button>
                <button
                  type="button"
                  onClick={() => dismiss()}
                  disabled={!hasContent || submitting}
                  className={`w-10 h-10 rounded-full flex items-center justify-center transition-all active:scale-90 ${
                    !hasContent || submitting
                      ? "bg-elevated text-dim cursor-not-allowed"
                      : "bg-indigo-500 hover:bg-indigo-400 text-white"
                  }`}
                  title="Save to inbox & close"
                >
                  <svg className="w-5 h-5" fill="none" stroke="currentColor" strokeWidth={2} viewBox="0 0 24 24">
                    <path strokeLinecap="round" strokeLinejoin="round" d="M20 13V6a2 2 0 00-2-2H6a2 2 0 00-2 2v7m16 0v5a2 2 0 01-2 2H6a2 2 0 01-2-2v-5m16 0h-2.586a1 1 0 00-.707.293l-2.414 2.414a1 1 0 01-.707.293h-2.172a1 1 0 01-.707-.293l-2.414-2.414A1 1 0 006.586 13H4" />
                  </svg>
                </button>
                <button
                  type="button"
                  onClick={launchAgent}
                  disabled={!hasContent || submitting || anyUploading}
                  className={`w-10 h-10 rounded-full flex items-center justify-center active:scale-90 transition-all ${
                    !project || !hasContent || submitting || anyUploading
                      ? "bg-elevated text-dim cursor-not-allowed"
                      : "bg-accent text-accent-ink hover:opacity-90"
                  }`}
                  title={project ? "Launch agent (⌘/Ctrl+Enter)" : "Pick a project to launch"}
                >
                  <svg className="w-5 h-5" fill="none" stroke="currentColor" strokeWidth={2} viewBox="0 0 24 24">
                    <path strokeLinecap="round" strokeLinejoin="round" d="M6 12L3.269 3.126A59.768 59.768 0 0121.485 12 59.77 59.77 0 013.27 20.876L5.999 12zm0 0h7.5" />
                  </svg>
                </button>
              </div>
          </form>
        </div>
      </div>
      {previewIndex != null && attachments.length > 0 && (
        <ImageLightbox
          media={attachments.filter(a => !a.uploading).map(a => ({
            src: a.previewUrl || (a.uploadedPath ? attachmentUrl(a.uploadedPath) : null),
            filename: a.originalName,
            type: "image",
          }))}
          initialIndex={Math.min(previewIndex, attachments.filter(a => !a.uploading).length - 1)}
          onClose={() => setPreviewIndex(null)}
        />
      )}
    </div>
  );
}
