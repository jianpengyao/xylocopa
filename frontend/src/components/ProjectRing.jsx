import FluentEmoji from "./FluentEmoji";
import { resolveProjectEmoji } from "../lib/projectEmoji";
import { rem } from "../lib/textScale";

/**
 * Project identity icon.
 *
 * Displays the user's chosen emoji. The folder glyph is special: whether
 * the user picked 📁 explicitly or left it as default, the render flips
 * to 📂 (open) when an agent is active, 📁 (closed) when idle.
 */
export default function ProjectRing({
  emoji,
  hasActiveAgents = false,
  size = 28,
  className = "",
}) {
  const displayEmoji = resolveProjectEmoji(emoji, { hasActiveAgents });

  return (
    <span
      className={`inline-flex items-center justify-center shrink-0 ${className}`}
      style={{ width: rem(size), height: rem(size) }}
    >
      <FluentEmoji char={displayEmoji} size={size} />
    </span>
  );
}
