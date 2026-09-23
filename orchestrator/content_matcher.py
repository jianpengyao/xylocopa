"""Content matcher for JSONL→DB user-turn promotion.

When the sync engine encounters a user turn in JSONL, it must decide
whether it is an echo of an existing web/task message (→ promote) or
genuinely new CLI input (→ create).

The JSONL records content *after* CLI/tmux processing, which may differ
from the web-originated DB content in several ways:

    - Task prompt wrapping:  _build_task_prompt adds ``# Task:``,
      ``## Before You Start`` (with insights), ``## Guidelines``
    - Whitespace normalization:  tmux converts tabs → spaces
    - Content truncation:  CLI may receive a truncated version
    - Paste wrapping:  CC >= 2.1.28x wraps tmux pastes in
      ``<pasted_content id=…>`` (unwrapped here and in the parser)

Note: the ``_build_agent_prompt`` wrapper (``You are working in project: …``)
is already stripped by ``parse_session_turns`` before content reaches here.
"""

import re

from jsonl_parser import unwrap_pasted_content
from models import Message

# ---------------------------------------------------------------------------
# Regexes for _strip_task_prompt
# ---------------------------------------------------------------------------

# Non-retry: # Task: {title}\n\n{description}\n\n## Before You Start …
_TASK_BODY_RE = re.compile(
    r"^# Task: [^\n]+\n\n"  # title + blank line
    r"(.*?)"  # description (captured)
    r"\n\n## (?:Before You Start|Guidelines)\b",
    re.DOTALL,
)

# Retry: … ## Original Task (Background Context)\n{description}\n\n## Instructions …
_RETRY_BODY_RE = re.compile(
    r"## Original Task[^\n]*\n"
    r"(.*?)"
    r"(?:\n\n## Instructions|\Z)",
    re.DOTALL,
)


class ContentMatcher:
    """Match JSONL user-turn content against queued web/task DB messages.

    Strategies (tried in order — first match wins):

    1. **exact**            — byte-for-byte content match
    2. **task-stripped**     — strip ``_build_task_prompt`` wrapper, then exact
    3. **normalized**        — whitespace-normalised match (tab→space etc.)
    4. **task-normalized**   — strip task wrapper + normalise
    5. **task-description-contained** — stripped description found inside candidate
       (handles retry prompts where display_content includes title + retry info)
    """

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @staticmethod
    def match(
        content: str,
        candidates: list[Message],
    ) -> tuple[Message | None, str]:
        """Find the best matching candidate for JSONL user-turn content.

        Args:
            content:    JSONL content (after ``strip_agent_preamble``).
            candidates: Unlinked web/task messages, ordered by ``created_at ASC``.

        Returns:
            ``(matched_message, method)`` or ``(None, "none")``.
        """
        if not candidates or not content:
            return None, "none"

        # 0. Claude Code paste wrapper — parse_session_turns already unwraps,
        #    but callers feeding raw JSONL text (or DB rows imported before
        #    the unwrap existed) must not fall through to "none".
        content = unwrap_pasted_content(content)

        # 1. Exact match — fastest, most certain
        for msg in candidates:
            if msg.content and msg.content == content:
                return msg, "exact"

        # 2. Task-stripped + exact match
        stripped = ContentMatcher.strip_task_prompt(content)
        is_task = stripped != content
        if is_task:
            for msg in candidates:
                if msg.content and msg.content == stripped:
                    return msg, "task-stripped"

        # 3. Normalised match (tab→space, trailing whitespace)
        norm = ContentMatcher.normalize(content)
        for msg in candidates:
            if msg.content and ContentMatcher.normalize(msg.content) == norm:
                return msg, "normalized"

        # 4. Task-stripped + normalised
        if is_task:
            norm_stripped = ContentMatcher.normalize(stripped)
            for msg in candidates:
                if msg.content and ContentMatcher.normalize(msg.content) == norm_stripped:
                    return msg, "task-normalized"

        # 5. Task description contained in candidate (retry dedup)
        #    For retry tasks, display_content = title + desc + "Retry attempt #N"
        #    + retry_context, while strip_task_prompt extracts only the
        #    description from "## Original Task".  Check if the stripped
        #    description is a substring of any candidate's content.
        if is_task and len(stripped) > 20:
            norm_stripped = ContentMatcher.normalize(stripped)
            for msg in candidates:
                if msg.content and norm_stripped in ContentMatcher.normalize(msg.content):
                    return msg, "task-description-contained"

        return None, "none"

    # ------------------------------------------------------------------
    # Helpers (public so tests and callers can use them directly)
    # ------------------------------------------------------------------

    @staticmethod
    def strip_task_prompt(content: str) -> str:
        """Strip ``_build_task_prompt`` wrapper → original description.

        Handles both normal and retry formats::

            # Normal
            # Task: {title}\\n\\n{desc}\\n\\n## Before You Start\\n…

            # Retry
            # Task: {title}\\n\\n## Your Focus …\\n## Original Task …\\n{desc}\\n\\n## Instructions …
        """
        if not content.startswith("# Task:"):
            return content

        # Retry first — "## Your Focus" distinguishes retry from non-retry.
        # Must check before _TASK_BODY_RE because that regex also matches
        # retry prompts (they have ## Before You Start at the end too),
        # capturing all retry sections as "description".
        if "## Your Focus" in content[:500]:
            m = _RETRY_BODY_RE.search(content)
            if m:
                return m.group(1).strip()

        # Non-retry: description sits between title and first ## section
        m = _TASK_BODY_RE.match(content)
        if m:
            return m.group(1).strip()

        return content

    @staticmethod
    def normalize(text: str) -> str:
        """Normalise whitespace for fuzzy matching.

        Handles tmux tab→space conversion, trailing whitespace, and
        inconsistent blank-line counts.
        """
        if not text:
            return ""
        # Collapse horizontal whitespace (tabs + spaces) → single space
        lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.split("\n")]
        result = "\n".join(lines).strip()
        # Collapse 3+ consecutive newlines → double
        result = re.sub(r"\n{3,}", "\n\n", result)
        return result
