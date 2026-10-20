// Suspense fallback for ProjectDetailPage. Header + filter tabs only —
// agent list area left blank so users don't see ghost-rows flash in.

export default function ProjectDetailSkeleton() {
  return (
    <div className="flex flex-col h-full bg-page">
      {/* Header */}
      <div className="shrink-0 bg-page border-b border-divider px-4 py-3 flex items-center gap-3">
        <div className="w-7 h-7 rounded bg-input animate-pulse" />
        <div className="h-5 w-32 rounded bg-input animate-pulse" />
        <div className="ml-auto h-7 w-7 rounded-full bg-input animate-pulse" />
      </div>
      {/* Filter tabs */}
      <div className="shrink-0 px-4 py-2 flex items-center gap-2">
        {[60, 70, 80, 70, 70, 70].map((w, i) => (
          <div key={i} className="h-9 rounded-full bg-input animate-pulse" style={{ width: w }} />
        ))}
      </div>
      {/* Empty body — no placeholder agent rows */}
      <div className="flex-1" />
    </div>
  );
}
