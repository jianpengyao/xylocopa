import { createContext, useContext, useState, useCallback, useEffect, useRef } from "react";

const ToastContext = createContext(null);

const ICON_COLORS = {
  success: "#34C759",
  error: "#FF3B30",
  warning: "#FF9500",
  info: "#007AFF",
};

const ICON_PATHS = {
  success: <path strokeLinecap="round" strokeLinejoin="round" d="M5 13l4 4L19 7" />,
  error: <path strokeLinecap="round" strokeLinejoin="round" d="M6 18L18 6M6 6l12 12" />,
  warning: <path strokeLinecap="round" strokeLinejoin="round" d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-3L13.732 4c-.77-1.333-2.694-1.333-3.464 0L3.34 16c-.77 1.333.192 3 1.732 3z" />,
  info: <path strokeLinecap="round" strokeLinejoin="round" d="M13 16h-1v-4h-1m1-4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />,
};

function ToastItem({ toast, onDismiss }) {
  const [leaving, setLeaving] = useState(false);

  useEffect(() => {
    const dur = toast.duration || 3000;
    const fadeTimer = setTimeout(() => setLeaving(true), dur - 400);
    const removeTimer = setTimeout(() => onDismiss(toast.id), dur);
    return () => { clearTimeout(fadeTimer); clearTimeout(removeTimer); };
  }, [toast.id, toast.duration, onDismiss]);

  const iconColor = ICON_COLORS[toast.type] || ICON_COLORS.info;

  return (
    <div
      className={`toast-pill ${leaving ? "toast-exit" : "toast-enter"}`}
      style={{
        background: "rgba(255,255,255,0.95)",
        backdropFilter: "blur(12px)",
        WebkitBackdropFilter: "blur(12px)",
        // rem, not px: follows Settings > Text size.
        borderRadius: "0.875rem",
        padding: "0.625rem 0.875rem",
        maxWidth: "18.75rem",
        boxShadow: "0 2px 16px rgba(0,0,0,0.12), 0 0 0 0.5px rgba(0,0,0,0.06)",
        display: "flex",
        alignItems: "center",
        gap: "0.5rem",
      }}
    >
      <svg
        width="18" height="18" viewBox="0 0 24 24"
        fill="none" stroke={iconColor} strokeWidth={2.5}
        style={{ flexShrink: 0, width: "1.125rem", height: "1.125rem" }}
      >
        {ICON_PATHS[toast.type] || ICON_PATHS.info}
      </svg>
      <span style={{ color: "#1c1c1e", fontSize: "0.8125rem", fontWeight: 500, lineHeight: 1.3, flex: 1 }}>
        {toast.message}
      </span>
    </div>
  );
}

export function ToastProvider({ children }) {
  const [toasts, setToasts] = useState([]);
  const idRef = useRef(0);

  const addToast = useCallback((message, type = "info", duration) => {
    const id = ++idRef.current;
    setToasts((prev) => [...prev, { id, message, type, duration }]);
    return id;
  }, []);

  const dismissToast = useCallback((id) => {
    setToasts((prev) => prev.filter((t) => t.id !== id));
  }, []);

  const toast = useCallback((message, type = "info", duration) => addToast(message, type, duration), [addToast]);
  toast.success = (msg, duration) => addToast(msg, "success", duration);
  toast.error = (msg, duration) => addToast(msg, "error", duration || 5000);
  toast.warning = (msg, duration) => addToast(msg, "warning", duration);
  toast.info = (msg, duration) => addToast(msg, "info", duration);

  return (
    <ToastContext.Provider value={toast}>
      {children}
      {toasts.length > 0 && (
        <div className="toast-container safe-area-toast">
          {toasts.map((t) => (
            <ToastItem key={t.id} toast={t} onDismiss={dismissToast} />
          ))}
        </div>
      )}
      <style>{`
        .toast-container {
          position: fixed;
          z-index: 9999;
          pointer-events: none;
          display: flex;
          flex-direction: column;
          align-items: center;
          gap: 8px;
          /* mobile: centered at top */
          left: 50%;
          transform: translateX(-50%);
        }
        @media (min-width: 640px) {
          /* desktop: top-right */
          .toast-container {
            left: auto;
            right: 16px;
            transform: none;
            align-items: flex-end;
          }
        }
        .toast-pill {
          pointer-events: auto;
        }
        @keyframes toast-slide-in {
          from { opacity: 0; transform: translateY(-12px) scale(0.96); }
          to   { opacity: 1; transform: translateY(0) scale(1); }
        }
        @keyframes toast-fade-out {
          from { opacity: 1; transform: translateY(0) scale(1); }
          to   { opacity: 0; transform: translateY(-8px) scale(0.96); }
        }
        .toast-enter {
          animation: toast-slide-in 0.3s cubic-bezier(0.2, 0.8, 0.2, 1) forwards;
        }
        .toast-exit {
          animation: toast-fade-out 0.35s cubic-bezier(0.4, 0, 1, 1) forwards;
        }
        .dark .toast-pill {
          background: rgba(44,44,46,0.92) !important;
          box-shadow: 0 2px 16px rgba(0,0,0,0.3), 0 0 0 0.5px rgba(255,255,255,0.08) !important;
        }
        .dark .toast-pill span {
          color: #f5f5f7 !important;
        }
      `}</style>
    </ToastContext.Provider>
  );
}

export function useToast() {
  const ctx = useContext(ToastContext);
  if (!ctx) throw new Error("useToast must be used within ToastProvider");
  return ctx;
}
