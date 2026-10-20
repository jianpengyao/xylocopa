// `?focus=<message_id>` target resolution for AgentChatPage.

/** Rows the chat merges into one ToolLogBubble when consecutive. */
export const isToolRow = (m) => (m?.role === "AGENT" && m.kind === "tool_use") || m?.kind === "tool_activity";

/**
 * The message id whose node carries `data-msg-id` for `msgId`. Consecutive
 * tool rows render as ONE ToolLogBubble keyed by the run's first id (the
 * toolGroups build in AgentChatPage, after dropping stop_hook rows), so a
 * later member has no node of its own — map it to the run leader. Any other
 * id (or one not loaded yet) maps to itself.
 */
export function focusTargetId(sentMessages, msgId) {
  const rows = (sentMessages || []).filter((m) => m.kind !== "stop_hook");
  let i = rows.findIndex((m) => m.id === msgId);
  if (i === -1 || !isToolRow(rows[i])) return msgId;
  while (i > 0 && isToolRow(rows[i - 1])) i--;
  return rows[i].id;
}
