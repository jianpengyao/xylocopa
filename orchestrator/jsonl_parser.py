"""JSONL session parser — parse Claude Code session JSONL into conversation turns.

Extracted from agent_dispatcher.py for testability and clean module boundaries.
All functions here are pure (no DB, no WebSocket) — they only read JSONL and
transform data.

Public API:
    parse_session_turns          — read + parse a JSONL file
    parse_session_turns_from_lines — parse pre-read lines
    is_wrapped_prompt            — detect system-wrapped prompts
    merge_interactive_meta       — merge JSONL + web-UI interactive metadata
    strip_agent_preamble         — remove orchestrator preamble/postamble
    unwrap_pasted_content        — remove Claude Code's <pasted_content> paste wrapper
    format_tool_summary          — format tool_use as one-line markdown
    parse_xylocopa_marker        — legacy marker parsing (also accepts old agenthive marker)
    derive_selected_index        — derive selection from answer text
"""

import copy
import hashlib
import json
import logging
import re

from skills import format_skill_summary, is_hidden_meta_entry, skill_turn_metadata
from utils import is_interrupt_message

logger = logging.getLogger("orchestrator.jsonl_parser")


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Marker prefixes — new code writes XYLOCOPA, parser also accepts the legacy
# agenthive prefix so old session JSONL files keep working unchanged.
XYLOCOPA_PROMPT_MARKER = "<!-- xylocopa-prompt"
AGENTHIVE_PROMPT_MARKER = "<!-- agenthive-prompt"  # legacy — still parsed for back-compat

# Preamble prefix used to detect system-wrapped prompts in JSONL content.
PREAMBLE_PREFIX = "You are working in project:"

_IMAGE_META_RE = re.compile(
    r"^\[Image: original \d+x\d+, displayed at \d+x\d+\."
)

_PREAMBLE_RE = re.compile(
    r"^(?:<!-- (?:xylocopa|agenthive)-prompt[^>]*-->\n)?"  # optional marker line (new or legacy)
    r"You are working in project: .+?\n"
    r"Project path: .+?\n\n"
    r"First read the project's CLAUDE\.md to understand project conventions\.\n"
    r"(?:Do NOT write to memory files[^\n]*\n)?"    # optional memory guard line
    r"(?:Relevant past insights[^\n]*\n(?:  - [^\n]*\n)*\n?)?"  # legacy insights position
    r"(?:## Recent conversation context[^\n]*\n(?:.*?\n)*?\n)?",  # optional history
    re.DOTALL,
)
_POSTAMBLE_RE = re.compile(
    r"(?:\n\n---\n"
    r"The following are past insights.*?)?"  # optional insights block
    r"\n\nIf you make code changes, commit with message format: \[scope\] short description$",
    re.DOTALL,
)

# Claude Code (2.1.28x, first seen in session JSONL on 2026-09-19) wraps a
# terminal paste before recording the user turn:
#
#     \n\n<pasted_content id="9671">\n{text}\n</pasted_content id="9671">\n
#
# The id is a per-process random token and the closing tag repeats it. Every
# message the orchestrator delivers through tmux send-keys is a paste from
# the CLI's point of view, so web/task prompts echo back wrapped. Left
# unwrapped they no longer content-match their SENT rows (ContentMatcher),
# the row never gets promoted, and a second source=cli bubble is created for
# every web message. Attributes on the closing tag are optional so the older
# bare ``<pasted_content>`` form unwraps too.
_PASTED_CONTENT_RE = re.compile(
    r"<pasted_content(?P<attrs>(?:\s[^>]*)?)>\n?"
    r"(?P<body>.*?)"
    r"\n?</pasted_content(?P=attrs)?>",
    re.DOTALL,
)

# Slash-command JSONL wrapper Claude Code emits when a `/cmd args` prompt is
# accepted; reduced to "/cmd args" so ContentMatcher's exact strategy matches
# it against the web row's literal user typing.
#
# Tag order has varied across CC versions: older releases emit
# <command-message> first, v2.1.140+ emits <command-name> first. We accept
# any order — three independent tag matches; require both name and message
# to be present (a stray <command-name>-only fragment must still be dropped
# so it falls to the skip-list below).
_CMD_NAME_TAG_RE = re.compile(r"<command-name>(.*?)</command-name>", re.DOTALL)
_CMD_MSG_TAG_RE = re.compile(r"<command-message>(.*?)</command-message>", re.DOTALL)
_CMD_ARGS_TAG_RE = re.compile(r"<command-args>(.*?)</command-args>", re.DOTALL)


def _parse_command_wrapper(stripped: str) -> tuple[str, str] | None:
    """Return ``(cmd_name, args)`` if ``stripped`` is a wrapped slash-command
    echo, else None. Order-agnostic; requires both name and message tags."""
    if not (stripped.startswith("<command-name>")
            or stripped.startswith("<command-message>")):
        return None
    m_name = _CMD_NAME_TAG_RE.search(stripped)
    m_msg = _CMD_MSG_TAG_RE.search(stripped)
    if not m_name or not m_msg:
        return None
    m_args = _CMD_ARGS_TAG_RE.search(stripped)
    return m_name.group(1).strip(), (m_args.group(1).strip() if m_args else "")


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _short_path(path: str) -> str:
    """Shorten a file path for display (last 2 components)."""
    parts = path.rstrip("/").split("/")
    if len(parts) <= 2:
        return path
    return "/".join(parts[-2:])


def _is_image_metadata(text: str) -> bool:
    """Return True if text is CLI-generated image metadata (not user-facing)."""
    return bool(_IMAGE_META_RE.match(text.strip()))


# ---------------------------------------------------------------------------
# Interactive metadata helpers
# ---------------------------------------------------------------------------

def derive_selected_index(item: dict) -> None:
    """Derive selected_index and selected_indices from an interactive item's answer text.

    For ask_user_question, matches the ="label" pattern against each question's options.
    Populates both selected_index (Q0, backward compat) and selected_indices (all Qs).
    For exit_plan_mode, uses keyword matching on the answer.
    """
    answer = item.get("answer")
    if not answer or not isinstance(answer, str):
        return
    # Skip dismissed/rejected answers — no valid selection to derive
    if (answer.startswith("The user doesn't want to proceed")
            or answer.startswith("User declined")
            or answer.startswith("Tool use rejected")):
        return
    if item.get("type") == "ask_user_question":
        questions = item.get("questions", [])
        if not questions:
            return
        # Find all ="label" patterns in order
        matches = re.findall(r'="([^"]+)"', answer)
        if not matches:
            return
        sel_indices = item.get("selected_indices", {})
        # Positional matching: consume match indices so duplicate labels
        # across questions don't cross-match.
        used_match_indices: set[int] = set()
        for qi, q in enumerate(questions):
            if sel_indices.get(str(qi)) is not None:
                continue  # Already set for this question
            options = q.get("options", [])
            for mi, label in enumerate(matches):
                if mi in used_match_indices:
                    continue
                for oi, opt in enumerate(options):
                    if opt.get("label") == label:
                        sel_indices[str(qi)] = oi
                        used_match_indices.add(mi)
                        break
                if sel_indices.get(str(qi)) is not None:
                    break
        if sel_indices:
            item["selected_indices"] = sel_indices
        # Backward compat: set selected_index from Q0
        if item.get("selected_index") is None and sel_indices.get("0") is not None:
            item["selected_index"] = sel_indices["0"]
    elif item.get("type") == "permission_prompt":
        if item.get("selected_index") is not None:
            return  # Already set
        # Simple exact-label match against question options
        questions = item.get("questions", [])
        if questions:
            options = questions[0].get("options", [])
            for oi, opt in enumerate(options):
                if opt.get("label", "").lower() == answer.lower().strip():
                    item["selected_index"] = oi
                    return
        # Keyword fallback
        a = answer.lower().strip()
        if a.startswith("yes") and "always" in a:
            item["selected_index"] = 1
        elif a.startswith("yes"):
            item["selected_index"] = 0
        elif a.startswith("no"):
            item["selected_index"] = 2
    elif item.get("type") == "exit_plan_mode":
        if item.get("selected_index") is not None:
            return  # Already set
        a = answer.lower().strip()
        # Dismissal / rejection — don't assign any index
        if (a.startswith("the user doesn't want to proceed")
                or a.startswith("user declined")
                or a.startswith("tool use rejected")):
            return
        # Exact label matching first (avoids keyword collision like "bypass manual")
        _PLAN_LABELS_LOWER = [
            "yes, bypass permissions",
            "yes, manual approval",
            "give feedback",
        ]
        for i, lbl in enumerate(_PLAN_LABELS_LOWER):
            if a == lbl:
                item["selected_index"] = i
                return
        # Keyword fallback for answers from Claude's tool_result (may differ in wording)
        if "bypass" in a and "manual" not in a:
            item["selected_index"] = 0
        elif "manual" in a:
            item["selected_index"] = 1
        elif "feedback" in a or "type here" in a:
            item["selected_index"] = 2
        # else: leave selected_index unset — don't default to 0


def merge_interactive_meta(db_meta_json: str | None, new_meta: dict | None) -> str | None:
    """Merge interactive metadata, preserving web-set answers during sync.

    When the web UI answers an interactive prompt via /api/agents/{id}/answer,
    _patch_interactive_answer() immediately stores selected_index + answer in
    the DB.  The sync loop later re-parses the JSONL and may overwrite the
    metadata with a version where answer is still null (Claude hasn't written
    the tool_result yet).  This function prevents that regression by keeping
    the DB's selected_index/answer when the JSONL version has answer=null.

    If the JSONL version has a non-null answer, it takes precedence (it's the
    authoritative response from Claude's actual tool_result).

    Returns a JSON string.  Does NOT mutate *new_meta*.
    """
    if new_meta is None:
        return db_meta_json  # Nothing to merge — keep existing
    if not db_meta_json:
        return json.dumps(new_meta)  # No existing — use new

    try:
        db_meta = json.loads(db_meta_json)
    except (json.JSONDecodeError, TypeError):
        logger.debug("Failed to parse existing meta_json during merge")
        return json.dumps(new_meta)

    db_items = {
        item.get("tool_use_id"): item
        for item in db_meta.get("interactive", [])
        if item.get("tool_use_id")
    }
    if not db_items:
        return json.dumps(new_meta)

    # Work on a copy so the caller's parsed turns are not mutated
    merged = copy.deepcopy(new_meta)

    for item in merged.get("interactive", []):
        tid = item.get("tool_use_id", "")
        db_item = db_items.get(tid)
        if not db_item:
            continue
        # Preserve auto_approved from DB — JSONL never has this flag,
        # it's set by PostToolUse hook or sync based on agent config.
        if db_item.get("auto_approved") and not item.get("auto_approved"):
            item["auto_approved"] = True
        # JSONL answer is null but DB has a web-set answer → preserve it
        if item.get("answer") is None and db_item.get("answer") is not None:
            item["answer"] = db_item["answer"]
            if db_item.get("selected_index") is not None:
                item["selected_index"] = db_item["selected_index"]
            if db_item.get("selected_indices"):
                item["selected_indices"] = db_item["selected_indices"]
        # JSONL has a real answer → authoritative. Carry selected_index from
        # DB since CC's tool_result doesn't always include the numeric index.
        elif item.get("answer") is not None:
            if db_item.get("selected_index") is not None:
                item["selected_index"] = db_item["selected_index"]
            if db_item.get("selected_indices"):
                item["selected_indices"] = db_item["selected_indices"]

    return json.dumps(merged)


# ---------------------------------------------------------------------------
# Prompt detection and stripping
# ---------------------------------------------------------------------------

def parse_xylocopa_marker(text: str) -> dict | None:
    """Extract agent_id and msg_id from a xylocopa-prompt marker.

    Also accepts the legacy ``<!-- agenthive-prompt`` prefix so JSONL
    files written before the rename keep deduping correctly.

    Returns dict of attributes if marker found, None otherwise.
    Old-format markers (no attributes) return an empty dict.
    """
    head = text[:200]
    pos = head.find(XYLOCOPA_PROMPT_MARKER)
    if pos >= 0:
        prefix = XYLOCOPA_PROMPT_MARKER
    else:
        pos = head.find(AGENTHIVE_PROMPT_MARKER)
        if pos < 0:
            return None
        prefix = AGENTHIVE_PROMPT_MARKER
    end = text.find("-->", pos)
    if end < 0:
        return {}
    attrs_str = text[pos + len(prefix):end]
    attrs: dict[str, str] = {}
    for part in attrs_str.split():
        if "=" in part:
            k, _, v = part.partition("=")
            attrs[k] = v
    return attrs


# Legacy alias — pre-rename code/tests import this name
parse_agenthive_marker = parse_xylocopa_marker


def unwrap_pasted_content(content: str) -> str:
    """Strip Claude Code's ``<pasted_content id=…>…</pasted_content id=…>``
    paste wrapper(s), returning the pasted text itself.

    Applies to every block in the message (a CLI user can paste several
    times into one prompt). Returns ``content`` unchanged when no wrapper
    is present, so callers can cheaply detect whether anything happened.
    """
    if not content or "<pasted_content" not in content:
        return content
    unwrapped = _PASTED_CONTENT_RE.sub(lambda m: m.group("body"), content)
    return unwrapped.strip() if unwrapped != content else content


def is_wrapped_prompt(content: str) -> bool:
    """Check if content is a system-wrapped prompt from _build_agent_prompt
    or _build_task_prompt.

    Detects:
    - Agent preamble: ``You are working in project:``
    - Marker: ``<!-- xylocopa-prompt`` (or legacy ``<!-- agenthive-prompt``)
    - Task prompt header: ``# Task:`` (remains after strip_agent_preamble)
    """
    head = unwrap_pasted_content(content)[:80]
    return (
        PREAMBLE_PREFIX in head
        or XYLOCOPA_PROMPT_MARKER in head
        or AGENTHIVE_PROMPT_MARKER in head
        or head.startswith("# Task:")
    )


def strip_agent_preamble(content: str) -> str:
    """Strip orchestrator-injected preamble/postamble from user messages.

    Unwraps Claude Code's ``<pasted_content>`` paste wrapper first — since
    CC 2.1.28x the whole orchestrator prompt arrives inside it, so the
    preamble regex would otherwise never anchor at the start of the text.
    """
    content = unwrap_pasted_content(content)
    text = _PREAMBLE_RE.sub("", content)
    text = _POSTAMBLE_RE.sub("", text)
    return text.strip() if text != content else content


# ---------------------------------------------------------------------------
# Tool summary formatting
# ---------------------------------------------------------------------------

def format_tool_summary(name: str, input_data: dict) -> str | None:
    """Format a tool call as a brief one-line markdown summary."""
    if name == "Bash":
        desc = input_data.get("description", "")
        if not desc:
            cmd = input_data.get("command", "")
            desc = cmd.split("\n")[0]
            if len(desc) > 60:
                desc = desc[:57] + "..."
        return f"> `Bash` {desc}"
    if name in ("Read", "Edit", "Write"):
        path = input_data.get("file_path", "")
        # Keep full path for media files so the frontend can preview them
        if path.lower().endswith(
            (".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp",
             ".mp4", ".webm", ".mov", ".csv")
        ):
            return f"> `{name}` {path}"
        return f"> `{name}` {_short_path(path)}"
    if name == "Grep":
        pat = input_data.get("pattern", "")
        if len(pat) > 40:
            pat = pat[:37] + "..."
        return f'> `Grep` "{pat}"'
    if name == "Glob":
        return f"> `Glob` {input_data.get('pattern', '')}"
    if name == "Task":
        return f"> `Task` {input_data.get('description', '')}"
    if name == "Skill":
        return format_skill_summary(input_data)
    # Skip noisy internal tools
    if name in ("ToolSearch",):
        return None
    return f"> `{name}`"


# ---------------------------------------------------------------------------
# JSONL session parsing
# ---------------------------------------------------------------------------

def parse_session_turns(
    jsonl_path: str,
    max_bytes: int = 0,
) -> list[tuple[str, str, dict | None, str | None, str | None]]:
    """Parse a Claude Code session JSONL into conversation turns.

    If *max_bytes* > 0 and the file exceeds that size, only the last
    *max_bytes* are read (aligned to the next complete line boundary).
    This prevents OOM on very large session files.
    """
    try:
        file_size = os.path.getsize(jsonl_path) if max_bytes > 0 else 0
    except OSError:
        file_size = 0

    try:
        with open(jsonl_path, "r", errors="replace") as f:
            if max_bytes > 0 and file_size > max_bytes:
                f.seek(file_size - max_bytes)
                f.readline()  # discard partial line
                logger.warning(
                    "JSONL file %s is %d bytes, reading last %d bytes only",
                    jsonl_path, file_size, max_bytes,
                )
            lines = f.readlines()
    except OSError as e:
        logger.warning("parse_session_turns: failed to read %s: %s", jsonl_path, e)
        return []

    # Drop incomplete last line (mid-write by Claude Code)
    if lines and not lines[-1].endswith("\n"):
        lines.pop()

    return parse_session_turns_from_lines(lines)


# Need os for parse_session_turns
import os


def parse_session_turns_from_lines(
    lines: list[str],
) -> list[tuple[str, str, dict | None, str | None, str | None, str | None]]:
    """Parse pre-read JSONL lines into fine-grained conversation turns.

    Returns a list of (role, content, metadata, jsonl_uuid, kind, timestamp) tuples:
    - role: "user", "assistant", or "system"
    - content: text content of the turn
    - metadata: dict with tool/interactive info, or None
    - jsonl_uuid: the JSONL entry's uuid for dedup
    - kind: "text", "tool_use", or None (interactive/user/system)
    - timestamp: ISO 8601 timestamp from the JSONL entry, or None

    Each text segment and tool call becomes its own turn, so the
    conversation timeline has fine granularity matching the live view.
    """
    turns: list[tuple[str, str, dict | None, str | None, str | None, str | None]] = []

    # Accumulate text blocks between tool calls
    text_parts: list[str] = []
    # Track interactive tool calls (AskUserQuestion / ExitPlanMode)
    pending_interactive: list[dict] = []
    # Map tool_use_id → interactive entry for matching tool_result answers
    interactive_by_id: dict[str, dict] = {}
    # UUID for the current accumulated text segment
    text_uuid: str | None = None
    # Timestamp for the current accumulated text segment
    text_ts: str | None = None

    def flush_text():
        """Emit accumulated text as a kind='text' turn."""
        nonlocal text_uuid, text_ts
        if not text_parts:
            return
        text = "\n\n".join(t.strip() for t in text_parts if t.strip())
        # Strip legacy markers
        text = re.sub(r"\n?EXIT_SUCCESS\s*$", "", text).strip()
        text = re.sub(r"\n?EXIT_FAILURE:?.*$", "", text).strip()
        if text:
            turns.append(("assistant", text, None, text_uuid, "text", text_ts))
        text_parts.clear()
        text_uuid = None
        text_ts = None

    def flush_all():
        """Flush text + pending interactive items."""
        flush_text()
        if pending_interactive:
            meta = {"interactive": list(pending_interactive)}
            # Deterministic UUID from first tool_use_id for dedup on restart
            _first_tid = pending_interactive[0].get("tool_use_id", "")
            _interactive_uuid = f"interactive-{_first_tid}" if _first_tid else None
            turns.append(("assistant", "", meta, _interactive_uuid, None, None))
            pending_interactive.clear()

    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            logger.debug("Skipped malformed JSONL line in parse_jsonl_to_turns")
            continue

        entry_type = entry.get("type")
        entry_uuid = entry.get("uuid")  # present on user/assistant entries
        entry_ts = entry.get("timestamp")  # ISO 8601, e.g. "2026-03-24T17:02:44.544Z"

        if entry_type == "user":
            msg = entry.get("message", {})
            content = msg.get("content", "")
            if is_hidden_meta_entry(entry):
                continue
            # Check for tool_result in list-type content
            if isinstance(content, list):
                has_tool_result = False
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") == "tool_result":
                        has_tool_result = True
                        tool_use_id = block.get("tool_use_id", "")
                        if tool_use_id in interactive_by_id:
                            result_content = block.get("content", "")
                            # Ensure answer is always a string (content
                            # can be a list of content blocks in the API)
                            if isinstance(result_content, list):
                                result_content = " ".join(
                                    b.get("text", "") if isinstance(b, dict) else str(b)
                                    for b in result_content
                                ).strip() or ""
                            # Faithful classification by *outcome*, not by
                            # tool name. The parser otherwise turns every
                            # AskUserQuestion/ExitPlanMode tool_use into an
                            # interactive card unconditionally — but a call
                            # rejected by CC's input-schema validation
                            # (InputValidationError — e.g. an AskUserQuestion
                            # whose question has <2 options) never reached the
                            # user: no card was shown, no PreToolUse hook fired.
                            # It is not an interactive card — it is just a
                            # failed tool call. Reclassify it as a normal
                            # tool_use turn (the same path every other tool
                            # takes) so it stays faithfully and completely in
                            # the timeline, but never as a clickable question.
                            # Not dropped (that would lose it); not a card (it
                            # never was one).
                            if (block.get("is_error")
                                    and "InputValidationError" in str(result_content)):
                                _rejected = interactive_by_id.pop(tool_use_id, None)
                                if (_rejected is not None
                                        and _rejected in pending_interactive):
                                    pending_interactive.remove(_rejected)
                                    flush_text()
                                    _rname = ("AskUserQuestion"
                                              if _rejected.get("type") == "ask_user_question"
                                              else "ExitPlanMode")
                                    turns.append((
                                        "assistant",
                                        f"> `{_rname}` — rejected: InputValidationError",
                                        {"tool_name": _rname,
                                         "tool_use_id": tool_use_id,
                                         "is_error": True},
                                        f"tool-{tool_use_id}",
                                        "tool_use",
                                        entry_ts,
                                    ))
                                continue
                            interactive_by_id[tool_use_id]["answer"] = result_content
                            derive_selected_index(interactive_by_id[tool_use_id])
                            # Flush so each interactive Q&A becomes its
                            # own message bubble.
                            flush_all()
                if has_tool_result:
                    continue
                # Extract text from list content blocks (e.g. interrupt messages)
                content = "\n".join(
                    b.get("text", "") for b in content
                    if isinstance(b, dict) and b.get("type") == "text"
                ).strip()
                if not content:
                    continue
            # Real user message = string content (not tool_result list)
            if isinstance(content, str) and content.strip():
                stripped = content.strip()
                # Claude Code paste wrapper (<pasted_content id=…>) — unwrap
                # before any prefix check below so a pasted web/task prompt
                # is classified and content-matched on its real text.
                stripped = unwrap_pasted_content(stripped)
                # Slash-command wrapper.  The <command-name>/<command-message>
                # prefixes are owned exclusively by _parse_command_wrapper —
                # they must NOT fall through to the system-injection skip-list
                # below, because that would silently drop any wrapper shape
                # we fail to recognize (e.g. when CC ships a new tag order
                # or renames a tag).  Parse failure inside the wrapper
                # namespace is logged so drift is observable instead of
                # vanishing into the skip path.
                if (stripped.startswith("<command-name>")
                        or stripped.startswith("<command-message>")):
                    _parsed = _parse_command_wrapper(stripped)
                    if _parsed:
                        _cmd, _args = _parsed
                        flush_all()
                        turns.append(("user", f"{_cmd} {_args}".rstrip(), None, entry_uuid, "slash_signal", entry_ts))
                    else:
                        logger.warning(
                            "jsonl_parser: unrecognized <command-*> wrapper shape, "
                            "dropping turn (uuid=%s shape=%r)",
                            entry_uuid, stripped[:200],
                        )
                    continue
                # System-injected messages — not real user input, drop silently
                # (by design; these are CC's local-command output / system
                # reminders / task notifications, none of which belong in the
                # conversation transcript).
                if (
                    stripped.startswith("<local-command-caveat>")
                    or stripped.startswith("<local-command-stdout>")
                    or stripped.startswith("<system-reminder>")
                    or stripped.startswith("<task-notification>")
                ):
                    continue
                # Interrupt marker → system message with kind="interrupt"
                if is_interrupt_message(stripped):
                    flush_all()
                    # Strip brackets for display: "[Request interrupted by user]" → "Request interrupted by user"
                    display = stripped[1:-1]
                    turns.append(("system", display, None, entry_uuid, "interrupt", entry_ts))
                    continue
                # Compact summary → system message instead of user
                if stripped.startswith(
                    "This session is being continued from a previous conversation"
                ):
                    flush_all()
                    _compact_uuid = f"sys-{hashlib.md5(content.encode()).hexdigest()[:16]}"
                    turns.append(("system", content, None, _compact_uuid, None, entry_ts))
                    continue
                flush_all()
                clean = strip_agent_preamble(stripped)
                turns.append(("user", clean, None, entry_uuid, None, entry_ts))

        elif entry_type == "assistant":
            msg = entry.get("message", {})
            # Skip subagent messages
            if entry.get("parent_tool_use_id"):
                continue
            # Skip CC's internal bootstrap synthetic ack
            # ("No response requested." paired with isMeta user
            # "Continue from where you left off."). model="<synthetic>"
            # is CC's sentinel — the meta user is already dropped by
            # is_hidden_meta_entry above, this drops the matching ack.
            if msg.get("model") == "<synthetic>":
                continue
            # Rate limit error → system message with kind="rate_limit"
            if entry.get("isApiErrorMessage") and entry.get("error") == "rate_limit":
                flush_all()
                _rl_texts = [
                    b.get("text", "") for b in msg.get("content", [])
                    if isinstance(b, dict) and b.get("type") == "text"
                ]
                _rl_display = " ".join(t.strip() for t in _rl_texts if t.strip()) or "Rate limit reached"
                turns.append(("system", _rl_display, None, entry_uuid, "rate_limit", entry_ts))
                continue
            for block in msg.get("content", []):
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text" and block.get("text", "").strip():
                    if not _is_image_metadata(block["text"]):
                        text_parts.append(block["text"])
                        if text_uuid is None and entry_uuid:
                            text_uuid = entry_uuid
                        if text_ts is None and entry_ts:
                            text_ts = entry_ts
                elif block.get("type") == "tool_use":
                    tool_name = block.get("name", "")
                    tool_input = block.get("input", {})
                    tool_use_id = block.get("id", "")

                    if tool_name in ("AskUserQuestion", "ExitPlanMode"):
                        # Interactive: flush text, set up interactive state
                        flush_text()
                        if tool_name == "AskUserQuestion":
                            entry_data = {
                                "type": "ask_user_question",
                                "tool_use_id": tool_use_id,
                                "questions": tool_input.get("questions", []),
                                "answer": None,
                            }
                        else:
                            entry_data = {
                                "type": "exit_plan_mode",
                                "tool_use_id": tool_use_id,
                                "allowedPrompts": tool_input.get("allowedPrompts", []),
                                "plan": tool_input.get("plan", ""),
                                "answer": None,
                            }
                        pending_interactive.append(entry_data)
                        interactive_by_id[tool_use_id] = entry_data
                    else:
                        # Regular tool: flush text, emit tool_use turn
                        flush_text()
                        summary = format_tool_summary(tool_name, tool_input)
                        if summary:
                            tool_uuid = f"tool-{tool_use_id}" if tool_use_id else None
                            tool_meta = {"tool_name": tool_name, "tool_use_id": tool_use_id}
                            if tool_name == "Skill":
                                tool_meta.update(skill_turn_metadata(tool_input))
                            turns.append(("assistant", summary, tool_meta, tool_uuid, "tool_use", entry_ts))

        elif entry_type == "system":
            subtype = entry.get("subtype", "")
            if subtype == "turn_duration":
                continue
            flush_all()
            content = entry.get("content", "")
            if subtype or content:
                label = content or subtype.replace("_", " ")
                _kind = "stop_hook" if subtype == "stop_hook_summary" else None
                # Use entry uuid when available (stop_hook_summary has one);
                # fall back to content-derived hash for other system entries.
                _sys_uuid = entry_uuid or f"sys-{hashlib.md5(label.encode()).hexdigest()[:16]}"
                turns.append(("system", label, None, _sys_uuid, _kind, entry_ts))

    # Flush remaining
    flush_all()

    # Deduplicate user turns by UUID only (content dedup is too aggressive —
    # the same message can legitimately appear before and after compact)
    if turns:
        seen_uuids: set[str] = set()
        deduped: list[tuple[str, str, dict | None, str | None, str | None, str | None]] = []
        for turn in turns:
            role = turn[0]
            uuid = turn[3] if len(turn) > 3 else None
            if role == "user" and uuid:
                if uuid in seen_uuids:
                    continue
                seen_uuids.add(uuid)
            deduped.append(turn)
        turns = deduped

    logger.debug("Parsed %d turns from %d lines: %s", len(turns), len(lines),
                 [(t[0], t[4] if len(t) > 4 else None, t[3][:20] if len(t) > 3 and t[3] else 'none') for t in turns[:10]])
    return turns


# ---------------------------------------------------------------------------
# Backward-compat aliases (underscore-prefixed names used by existing code)
# ---------------------------------------------------------------------------

_parse_session_turns = parse_session_turns
_parse_session_turns_from_lines = parse_session_turns_from_lines
_is_wrapped_prompt = is_wrapped_prompt
_merge_interactive_meta = merge_interactive_meta
_strip_agent_preamble = strip_agent_preamble
_unwrap_pasted_content = unwrap_pasted_content
_format_tool_summary = format_tool_summary
_parse_xylocopa_marker = parse_xylocopa_marker
_parse_agenthive_marker = parse_agenthive_marker  # legacy alias
_derive_selected_index = derive_selected_index
_XYLOCOPA_PROMPT_MARKER = XYLOCOPA_PROMPT_MARKER
_AGENTHIVE_PROMPT_MARKER = AGENTHIVE_PROMPT_MARKER  # legacy alias
_PREAMBLE_PREFIX = PREAMBLE_PREFIX
