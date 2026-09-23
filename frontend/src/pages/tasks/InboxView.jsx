import { useState, useCallback, useMemo, useRef } from "react";
import { DndContext, closestCenter, PointerSensor, TouchSensor, useSensor, useSensors, DragOverlay } from "@dnd-kit/core";
import { SortableContext, useSortable, verticalListSortingStrategy, arrayMove, defaultAnimateLayoutChanges } from "@dnd-kit/sortable";
import { CSS } from "@dnd-kit/utilities";
import { restrictToVerticalAxis } from "@dnd-kit/modifiers";
import InboxCard from "../../components/cards/InboxCard";
import { reorderTasks } from "../../lib/api";
import useDraft from "../../hooks/useDraft";

/** Disable layout animation on drop to prevent snap-back */
function noDropAnimation(args) {
  if (args.wasDragging) return false;
  return defaultAnimateLayoutChanges(args);
}

function SortableTaskCard(props) {
  const { attributes, listeners, setNodeRef, transform, transition, isDragging } = useSortable({
    id: props.task.id,
    animateLayoutChanges: noDropAnimation,
  });
  const isGroupDragged = props.isGroupDragged && !isDragging;
  const style = {
    transform: CSS.Transform.toString(transform),
    transition,
    opacity: isDragging || isGroupDragged ? 0.3 : 1,
    ...(!props.expanded && { WebkitUserSelect: "none", userSelect: "none" }),
  };
  return (
    <div ref={setNodeRef} style={style}>
      <InboxCard {...props} dragHandleProps={{ listeners, attributes }} />
    </div>
  );
}

export default function InboxView({ tasks, loading, selecting, selected, onToggle, onEnterSelect, expandedTaskId, onExpandTask, onRefresh, sortMode = "custom" }) {
  // Optimistic order: local override until next props update (custom sort only)
  const [optimisticIds, setOptimisticIds] = useState(null);
  const prevTasksRef = useRef(tasks);
  const [showDeferred, setShowDeferred] = useDraft("ui:inbox:showDeferred", false);
  // Clear optimistic state when tasks prop changes (server data arrived)
  if (tasks !== prevTasksRef.current) {
    prevTasksRef.current = tasks;
    if (optimisticIds) setOptimisticIds(null);
  }

  // Split tasks into active and deferred. Truthy `deferred_to` is the
  // source of truth: the dispatcher sweep
  // (agent_dispatcher._sweep_expired_defers) clears it on expiry and emits
  // task_update, so we don't need a `> now` time comparison here.
  const { activeTasks, deferredTasks } = useMemo(() => {
    const active = [];
    const deferred = [];
    for (const t of tasks) {
      if (t.deferred_to) {
        deferred.push(t);
      } else {
        active.push(t);
      }
    }
    // Sort deferred by deferred_to ascending (earliest first)
    deferred.sort((a, b) => new Date(a.deferred_to) - new Date(b.deferred_to));
    return { activeTasks: active, deferredTasks: deferred };
  }, [tasks]);

  const sorted = useMemo(() => {
    switch (sortMode) {
      case "newest":
        return [...activeTasks].sort((a, b) => new Date(b.created_at) - new Date(a.created_at));
      case "oldest":
        return [...activeTasks].sort((a, b) => new Date(a.created_at) - new Date(b.created_at));
      case "name-asc":
        return [...activeTasks].sort((a, b) => (a.title || "").localeCompare(b.title || ""));
      case "name-desc":
        return [...activeTasks].sort((a, b) => (b.title || "").localeCompare(a.title || ""));
      case "custom":
      default: {
        const serverSorted = [...activeTasks].sort((a, b) => {
          if (a.sort_order !== b.sort_order) return a.sort_order - b.sort_order;
          return new Date(b.created_at) - new Date(a.created_at);
        });
        if (!optimisticIds) return serverSorted;
        const map = Object.fromEntries(serverSorted.map(t => [t.id, t]));
        return optimisticIds.map(id => map[id]).filter(Boolean);
      }
    }
  }, [activeTasks, sortMode, optimisticIds]);

  const [activeDragId, setActiveDragId] = useState(null);

  // Is the dragged item part of a multi-selection?
  const isMultiDrag = activeDragId && selecting && selected.has(activeDragId) && selected.size > 1;

  const isCustom = sortMode === "custom";

  const sensors = useSensors(
    useSensor(PointerSensor, { activationConstraint: { distance: 8 } }),
    useSensor(TouchSensor, { activationConstraint: { delay: 350, tolerance: 10 } }),
  );

  const handleDragStart = useCallback((event) => {
    if (navigator.vibrate) navigator.vibrate(10);
    setActiveDragId(event.active.id);
  }, []);

  const handleDragEnd = useCallback((event) => {
    const { active, over } = event;
    if (!over || active.id === over.id) {
      setActiveDragId(null);
      return;
    }

    const ids = sorted.map(t => t.id);
    let newIds;

    // Multi-drag: move selected group to the drop position
    if (selecting && selected.has(active.id) && selected.size > 1) {
      const groupIds = sorted.filter(t => selected.has(t.id)).map(t => t.id);
      const rest = ids.filter(id => !selected.has(id));
      let insertIdx = rest.indexOf(over.id);
      if (insertIdx === -1) insertIdx = rest.length;
      const activeOrigIdx = ids.indexOf(active.id);
      const overOrigIdx = ids.indexOf(over.id);
      if (activeOrigIdx < overOrigIdx) insertIdx += 1;
      newIds = [...rest.slice(0, insertIdx), ...groupIds, ...rest.slice(insertIdx)];
    } else {
      // Single drag
      const oldIdx = ids.indexOf(active.id);
      const newIdx = ids.indexOf(over.id);
      if (oldIdx === -1 || newIdx === -1) { setActiveDragId(null); return; }
      newIds = arrayMove(ids, oldIdx, newIdx);
    }

    // Optimistic update + clear drag in same batch — no snap-back
    setOptimisticIds(newIds);
    setActiveDragId(null);

    // Persist to server in background, then refresh
    reorderTasks(newIds).then(() => onRefresh?.());
  }, [sorted, selecting, selected, onRefresh]);

  const handleDragCancel = useCallback(() => setActiveDragId(null), []);

  if (!loading && sorted.length === 0 && deferredTasks.length === 0) {
    return (
      <div className="flex flex-col items-center justify-center py-16 text-faint">
        <svg className="w-10 h-10 mb-2" fill="none" stroke="currentColor" strokeWidth={1.5} viewBox="0 0 24 24">
          <path strokeLinecap="round" strokeLinejoin="round" d="M20 13V6a2 2 0 00-2-2H6a2 2 0 00-2 2v7m16 0v5a2 2 0 01-2 2H6a2 2 0 01-2-2v-5m16 0h-2.586a1 1 0 00-.707.293l-2.414 2.414a1 1 0 01-.707.293h-3.172a1 1 0 01-.707-.293l-2.414-2.414A1 1 0 006.586 13H4" />
        </svg>
        <p className="text-sm font-medium">Inbox zero</p>
        <p className="text-xs mt-1">Tap + to create a new task</p>
      </div>
    );
  }

  const activeTask = activeDragId ? sorted.find(t => t.id === activeDragId) : null;

  const deferredSection = deferredTasks.length > 0 && (
    <div className="mt-6">
      <button
        type="button"
        onClick={() => setShowDeferred(v => !v)}
        className="flex items-center justify-center w-full text-sm text-faint hover:text-dim transition-colors"
      >
        <span className="relative">
          <svg className={`absolute right-full top-1/2 -translate-y-1/2 mr-1.5 w-3.5 h-3.5 transition-transform ${showDeferred ? "rotate-90" : ""}`} fill="none" stroke="currentColor" strokeWidth={2} viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" d="M9 5l7 7-7 7" />
          </svg>
          Deferred ({deferredTasks.length})
        </span>
      </button>
      {showDeferred && (
        <div className="space-y-3 mt-3">
          {deferredTasks.map((task) => (
            <InboxCard
              key={task.id}
              task={task}
              selecting={selecting}
              selected={selected.has(task.id)}
              onToggle={onToggle}
              onEnterSelect={onEnterSelect}
              expanded={expandedTaskId === task.id}
              onExpand={onExpandTask}
              onRefresh={onRefresh}
            />
          ))}
        </div>
      )}
    </div>
  );

  // Non-custom sort: plain list without DnD
  if (!isCustom) {
    return (
      <>
        <div className="space-y-3">
          {sorted.map((task) => (
            <InboxCard
              key={task.id}
              task={task}
              selecting={selecting}
              selected={selected.has(task.id)}
              onToggle={onToggle}
              onEnterSelect={onEnterSelect}
              expanded={expandedTaskId === task.id}
              onExpand={onExpandTask}
              onRefresh={onRefresh}
            />
          ))}
        </div>
        {deferredSection}
      </>
    );
  }

  // Custom sort: DnD with drag handles
  return (
    <>
      <DndContext
        sensors={sensors}
        collisionDetection={closestCenter}
        modifiers={[restrictToVerticalAxis]}
        onDragStart={handleDragStart}
        onDragEnd={handleDragEnd}
        onDragCancel={handleDragCancel}
      >
        <SortableContext items={sorted.map(t => t.id)} strategy={verticalListSortingStrategy}>
          <div className="space-y-3">
            {sorted.map((task) => (
              <SortableTaskCard
                key={task.id}
                task={task}
                selecting={selecting}
                selected={selected.has(task.id)}
                onToggle={onToggle}
                onEnterSelect={onEnterSelect}
                expanded={!activeDragId && expandedTaskId === task.id}
                onExpand={onExpandTask}
                onRefresh={onRefresh}
                isGroupDragged={isMultiDrag && selected.has(task.id) && task.id !== activeDragId}
              />
            ))}
          </div>
        </SortableContext>
        <DragOverlay dropAnimation={null}>
          {activeTask ? (
            <div className="relative opacity-90 scale-[1.02] shadow-xl rounded-xl">
              {isMultiDrag && (
                <>
                  <div className="absolute inset-0 bg-surface rounded-xl ring-1 ring-edge/20 -rotate-1 translate-y-1 -z-10" />
                  <div className="absolute inset-0 bg-surface rounded-xl ring-1 ring-edge/10 rotate-1 translate-y-2 -z-20" />
                </>
              )}
              <InboxCard
                task={activeTask}
                selecting={selecting}
                selected={false}
                onToggle={() => {}}
                expanded={false}
                onExpand={() => {}}
                onRefresh={() => {}}
              />
              {isMultiDrag && (
                <div className="absolute -top-2 -right-2 z-10 w-6 h-6 rounded-full bg-accent text-white text-xs font-bold flex items-center justify-center shadow-md">
                  {selected.size}
                </div>
              )}
            </div>
          ) : null}
        </DragOverlay>
      </DndContext>
      {deferredSection}
    </>
  );
}
