"""claude.ai artifacts as project assets (the Artifact tool; used by asset_index.py).

An Artifact call reaches the DB only as a bare ``> `Artifact``` tool row
(metadata {tool_name, tool_use_id}). Its URL, title and type live in the
session transcript: the user entry carrying the tool_result also has a
top-level ``toolUseResult`` dict. This module reads that once per call into
``artifact_calls`` (models.ArtifactCall) — a memo that survives an
ASSET_INDEX_VERSION bump and the transcript's deletion — and turns calls into
asset refs:

  artifact — one ref per message row carrying a successful publish call
             (fork / subagent copies of a call each get their own row)
  text     — agent prose mentioning a key already published in that project

The key is ``artifact:<canonical URL of the first successful publish>``,
written once and never recomputed (asset_marks key on it). Later URL forms of
the same artifact (code-form uuid URL, toolUseResult.artifact_id) map onto it
through the alias map. Only a confirmed publish creates a key; prose never
does, which keeps Artifact *type* URLs (same shape) out by construction.

No module-level imports of config / session_cache / agent_dispatcher: tests
patch ``_resolve_jsonl`` and ``_backup_twin`` and must never reach real
transcripts. URLs are never logged above DEBUG.
"""

import glob
import json
import logging
import os
import re
import time
from collections import Counter
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

ARTIFACT_PREFIX = "artifact:"
# Bump only together with asset_index.ASSET_INDEX_VERSION (which forces the
# full run) when a classify_call fix needs old calls extracted again.
ARTIFACT_EXTRACT_VERSION = 1
# A call whose tool_use is in the transcript but whose result never showed up
# (agent killed mid-call) stops being retried this long after its message.
ARTIFACT_PENDING_TTL_S = 3600

_TITLE_MAX = 300
_DESC_MAX = 1000

_URL_PAT = r"https://claude\.ai/(?:code/)?artifact/[0-9A-Za-z][0-9A-Za-z-]{9,63}(?![0-9A-Za-z-])"
# The id class stops at ? # / ) * and CJK punctuation, so a query, a
# fragment, markdown and "。" / "：" never become part of the id.
_URL_RE = re.compile(_URL_PAT, re.I)
_CANON_RE = re.compile(
    r"https://claude\.ai/(code/)?artifact/([0-9A-Za-z][0-9A-Za-z-]{9,63})(?:[/?#].*)?",
    re.I | re.S,
)
_UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)
# Success sentence of a publish result. re.search: a "[Version … was
# published after …]" banner can precede it.
_OK_RE = re.compile(
    r"(?:Created a new Artifact at|Updated the Artifact at|Published .*? at)\s+"
    r"((?i:" + _URL_PAT + r"))"
)
_VERSION_N_RE = re.compile(r"\(Version (\d+),")
# "name:" inside the leading YAML frontmatter of a create-from-type result's
# instructions ("name: slides").
_FRONTMATTER_NAME_RE = re.compile(r"\A---[ \t]*\n(?:(?!---)[^\n]*\n)*?name:[ \t]*([^\n]+?)[ \t]*\n")

_READ_ACTIONS = frozenset({"quickstart", "read", "list", "open", "pin", "unpin"})
_ASSET_RESULT_KEYS = ("asset_upload", "asset_uploads", "asset_copy", "asset_delete")

_COLS = (
    "tool_use_id", "message_id", "agent_id", "created_at", "indexed_at", "outcome",
    "action", "artifact_key", "url", "artifact_id", "title", "description",
    "type_url", "type_name", "types_json", "version", "seq", "extract_version",
)
_UPSERT_SQL = (
    "INSERT INTO artifact_calls (%s) VALUES (%s) ON CONFLICT(tool_use_id) DO UPDATE SET %s" % (
        ", ".join(_COLS), ", ".join("?" * len(_COLS)),
        ", ".join(f"{c} = excluded.{c}" for c in _COLS if c not in ("tool_use_id", "indexed_at")),
    )
)


# ---------------------------------------------------------------------------
# URLs, keys, timestamps
# ---------------------------------------------------------------------------

def canonical_url(raw) -> str | None:
    """claude.ai artifact URL → canonical form, else None.

    Lower-cases scheme, host and the fixed path prefix; lower-cases the id
    only when it is a uuid (short base62 ids are case-sensitive); drops the
    query, fragment and anything after the id."""
    if not isinstance(raw, str):
        return None
    m = _CANON_RE.fullmatch(raw.strip())
    if not m:
        return None
    code, ident = m.groups()
    if _UUID_RE.fullmatch(ident):
        ident = ident.lower()
    return f"https://claude.ai/{'code/' if code else ''}artifact/{ident}"


def uuid_of(url: str | None) -> str | None:
    """The id of a canonical URL when it is uuid-shaped, else None."""
    if not url:
        return None
    ident = url.rsplit("/", 1)[-1]
    return ident.lower() if _UUID_RE.fullmatch(ident) else None


def has_hint(lower: str) -> bool:
    """Cheap prefilter on lower-cased text."""
    return "claude.ai/" in lower


def extract_urls(text: str | None) -> list[str]:
    """Canonical artifact URLs in text, deduped, in order of first appearance."""
    if not text or not has_hint(text.lower()):
        return []
    out: list[str] = []
    for m in _URL_RE.finditer(text):
        u = canonical_url(m.group(0))
        if u and u not in out:
            out.append(u)
    return out


def is_canonical_key(path: str) -> bool:
    if not path.startswith(ARTIFACT_PREFIX):
        return False
    u = path[len(ARTIFACT_PREFIX):]
    return canonical_url(u) == u


def _aliases(url: str | None, artifact_id=None) -> set[str]:
    out = set()
    if url:
        out.add(url)
        if uuid_of(url):
            out.add(uuid_of(url))
    if isinstance(artifact_id, str) and _UUID_RE.fullmatch(artifact_id):
        out.add(artifact_id.lower())
    return out


def _db_ts(dt: datetime) -> str:
    """SQLAlchemy's SQLite DateTime storage format (naive UTC). Raw DBAPI
    writes go through this: binding an aware datetime would use the
    deprecated default adapter and store a +00:00 suffix."""
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt.strftime("%Y-%m-%d %H:%M:%S.%f")


def _parse_ts(value) -> datetime | None:
    """DB / ISO timestamp → naive UTC datetime, or None."""
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def kind_of(type_url: str | None, type_name: str | None) -> str:
    """slides | doc | dashboard | page | other (the tile glyph / label)."""
    if not type_url:
        return "page"
    name = (type_name or "").lower()
    if "slide" in name or "deck" in name:
        return "slides"
    if "dashboard" in name:
        return "dashboard"
    if "doc" in name:
        return "doc"
    return "other"


# ---------------------------------------------------------------------------
# Classification (pure)
# ---------------------------------------------------------------------------

def _clip(value, n: int) -> str | None:
    return value[:n] if isinstance(value, str) and value else None


def _type_pairs(tur) -> list[list]:
    """Every [type_url, title] in a toolUseResult, at any depth: dicts with a
    type_url key (titled by a sibling title, or by type when it is a string —
    quickstart.match, design_systems) and type: {url, title} dicts (create,
    update, files_read / file_read / file_list)."""
    out: list[list] = []
    seen: set[str] = set()

    def add(url, title):
        u = canonical_url(url)
        if u and u not in seen:
            seen.add(u)
            out.append([u, title if isinstance(title, str) and title else None])

    def walk(o, depth):
        if depth > 8:
            return
        if isinstance(o, dict):
            if "type_url" in o:
                t = o.get("title")
                if not isinstance(t, str) and isinstance(o.get("type"), str):
                    t = o.get("type")
                add(o.get("type_url"), t)
            ty = o.get("type")
            if isinstance(ty, dict) and "url" in ty:
                add(ty.get("url"), ty.get("title"))
            for v in o.values():
                walk(v, depth + 1)
        elif isinstance(o, list):
            for v in o:
                walk(v, depth + 1)

    walk(tur, 0)
    return out


def _result_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            b["text"] for b in content
            if isinstance(b, dict) and b.get("type") == "text" and isinstance(b.get("text"), str)
        )
    return ""


def classify_call(inp, res, known_types=()) -> dict:
    """One Artifact call → {outcome, reason, action, url, artifact_id, title,
    description, type_url, type_name, types, version, seq}, plus target_url
    (the input url of an update) on a publish.

    inp: the tool_use input; res: {"is_error", "text"[, "toolUseResult"]} —
    the toolUseResult key only when the result entry had one. A publish
    needs both toolUseResult.url and a success sentence naming that URL
    (or, with no toolUseResult at all, the sentence alone)."""
    inp = inp if isinstance(inp, dict) else {}
    action = inp.get("action") if isinstance(inp.get("action"), str) else None
    action = (action or "publish")[:20]
    out = {
        "outcome": "ignored", "reason": None, "action": action, "url": None,
        "artifact_id": None, "title": None, "description": None, "type_url": None,
        "type_name": None, "types": [], "version": None, "seq": None,
    }
    text = res.get("text") or ""
    has_tur = "toolUseResult" in res
    tur = res.get("toolUseResult")
    if isinstance(tur, dict):
        out["types"] = _type_pairs(tur)

    if res.get("is_error") or text.lstrip().startswith("<tool_use_error>") or isinstance(tur, str):
        return {**out, "outcome": "error"}
    if action in _READ_ACTIONS:
        return {**out, "reason": "read"}
    if action == "delete":
        if inp.get("path") or inp.get("paths"):
            return {**out, "outcome": "asset"}
        url = canonical_url(inp.get("url"))
        if not url:
            return {**out, "reason": "no_url"}
        return {**out, "outcome": "deleted", "url": url}
    if action != "publish":
        return {**out, "reason": "action"}
    if inp.get("asset") is True or (
            isinstance(tur, dict) and any(k in tur for k in _ASSET_RESULT_KEYS)):
        return {**out, "outcome": "asset"}

    m = _OK_RE.search(text)
    ok_url = canonical_url(m.group(1)) if m else None
    if isinstance(tur, dict) and tur.get("url"):
        url = canonical_url(tur.get("url"))
        if not url or ok_url != url:
            return {**out, "reason": "unconfirmed"}
    elif not has_tur and ok_url:
        url, tur = ok_url, {}
    else:
        return {**out, "reason": "no_url"}

    denied = {canonical_url(t) for t in known_types} | {p[0] for p in out["types"]}
    if url in denied:
        return {**out, "reason": "type_url"}

    ty = tur.get("type")
    type_url = canonical_url(ty.get("url")) if isinstance(ty, dict) else None
    type_url = type_url or canonical_url(inp.get("type_url"))
    type_name = _clip(ty.get("title"), 100) if isinstance(ty, dict) else None
    if not type_name and tur.get("created_from_type") and isinstance(tur.get("instructions"), str):
        fm = _FRONTMATTER_NAME_RE.match(tur["instructions"])
        if fm:
            type_name = _clip(fm.group(1).strip("\"'"), 100)
    seq = tur.get("seq")
    if not isinstance(seq, int) or isinstance(seq, bool):
        vm = _VERSION_N_RE.search(text)
        seq = int(vm.group(1)) if vm else None
    aid = tur.get("artifact_id")
    # An update-by-url names the artifact it updates: an alias only (never
    # stored as the call's url), so a result in another URL form still
    # joins the key of the form the agent passed in.
    target = canonical_url(inp.get("url"))
    return {
        **out,
        "outcome": "publish",
        "url": url,
        "target_url": target if target not in denied else None,
        "artifact_id": _clip(aid, 64),
        "title": _clip(tur.get("title"), _TITLE_MAX) or _clip(inp.get("title"), _TITLE_MAX),
        "description": _clip(inp.get("description"), _DESC_MAX),
        "type_url": type_url,
        "type_name": type_name,
        "version": _clip(tur.get("version"), 40),
        "seq": seq,
    }


# ---------------------------------------------------------------------------
# Transcripts
# ---------------------------------------------------------------------------

def _resolve_jsonl(session_id: str, project_path: str, worktree: str | None = None,
                   cwd: str | None = None) -> str:
    """agent_dispatcher._resolve_session_jsonl (lazy: heavy module). The single
    point tests patch. May return a predicted path that does not exist."""
    from agent_dispatcher import _resolve_session_jsonl
    return _resolve_session_jsonl(session_id, project_path, worktree, cwd)


def _backup_twin(path: str) -> str | None:
    """<CLAUDE_HOME>/projects/<rel> → the session_cache backup copy (same rel)."""
    import session_cache
    base = os.path.join(session_cache.CLAUDE_HOME, "projects") + os.sep
    if not path.startswith(base):
        return None
    return os.path.join(session_cache.CACHE_DIR, path[len(base):])


class _Transcripts:
    """Candidate transcript files per agent, cached for one run."""

    def __init__(self, conn, agents: dict, project_paths: dict):
        self.conn = conn
        self.agents = agents  # id → (project, worktree, session_id, is_sub, parent_id, cid)
        self.project_paths = project_paths
        self._cache: dict[str, list[str]] = {}

    def _sessions(self, aid: str) -> list[tuple]:
        """[(sid, project_path, worktree, cwd)]: the current session first,
        then cc_sessions rotations newest first."""
        ag = self.agents.get(aid)
        if not ag:
            return []
        proj_path = self.project_paths.get(ag[0])
        if not proj_path:
            return []
        rows = [r for r in self.conn.execute(
            "SELECT session_id, project_path, worktree FROM cc_sessions "
            "WHERE agent_id = ? AND is_subagent_session = 0 ORDER BY started_at DESC",
            (aid,),
        ) if r[0]]
        if ag[2]:
            cur = next((r for r in rows if r[0] == ag[2]), (ag[2], None, None))
            rows = [cur] + [r for r in rows if r[0] != ag[2]]
        out, seen = [], set()
        for sid, cwd, wt in rows:
            if sid not in seen:
                seen.add(sid)
                # the session's recorded path doubles as the resolver's cwd
                # fallback (used only when it differs from the project path)
                out.append((sid, proj_path, wt or ag[1], cwd))
        return out

    def _owner(self, aid: str) -> str:
        seen = set()
        while aid not in seen:
            seen.add(aid)
            ag = self.agents.get(aid)
            if not ag or not ag[3] or not ag[4]:
                return aid
            aid = ag[4]
        return aid

    def files(self, aid: str) -> list[str]:
        if aid in self._cache:
            return self._cache[aid]
        out: list[str] = []
        ag = self.agents.get(aid)
        try:
            if ag and ag[3] and ag[4] and ag[5]:
                # Subagent: <parent session dir>/<psid>/subagents/[workflows/<wf>/]
                # agent-<cid>.jsonl. Entries inside carry the PARENT's sessionId.
                for sid, proj_path, wt, cwd in self._sessions(self._owner(aid)):
                    src = _resolve_jsonl(sid, proj_path, wt, cwd)
                    for base in (src, _backup_twin(src)):
                        if not base:
                            continue
                        pattern = os.path.join(
                            glob.escape(os.path.dirname(base)), glob.escape(sid), "subagents",
                            "**", glob.escape(f"agent-{ag[5]}.jsonl"),
                        )
                        hits = sorted(p for p in glob.glob(pattern, recursive=True)
                                      if os.path.isfile(p))
                        if hits:
                            out.extend(p for p in hits if p not in out)
                            break
            elif ag:
                for sid, proj_path, wt, cwd in self._sessions(aid):
                    src = _resolve_jsonl(sid, proj_path, wt, cwd)
                    if os.path.isfile(src):
                        path = src
                    else:
                        twin = _backup_twin(src)
                        path = twin if twin and os.path.isfile(twin) else None
                    if path and path not in out:
                        out.append(path)
        except (OSError, ValueError, TypeError):
            logger.debug("artifact_index: transcript lookup failed for agent %s", aid,
                         exc_info=True)
        self._cache[aid] = out
        return out


def _walk_entry(entry, tids: set, uses: dict, results: dict) -> None:
    if not isinstance(entry, dict):
        return
    msg = entry.get("message")
    content = msg.get("content") if isinstance(msg, dict) else None
    if not isinstance(content, list):
        return
    for block in content:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "tool_use":
            tid = block.get("id")
            if isinstance(tid, str) and tid in tids and tid not in uses:
                inp = block.get("input")
                uses[tid] = inp if isinstance(inp, dict) else {}
        elif btype == "tool_result":
            tid = block.get("tool_use_id")
            if not isinstance(tid, str) or tid not in tids:
                continue
            prev = results.get(tid)
            if prev is not None and isinstance(prev.get("toolUseResult"), dict):
                continue  # a call can have 2-3 result entries; one carries the dict
            res = {"is_error": bool(block.get("is_error")),
                   "text": _result_text(block.get("content"))}
            if "toolUseResult" in entry:
                res["toolUseResult"] = entry["toolUseResult"]
            if prev is None or isinstance(res.get("toolUseResult"), dict):
                results[tid] = res


def scan_transcripts(plan: dict[str, list[str]]) -> tuple[dict, dict, set, int]:
    """plan: tool_use_id → candidate files (in preference order).

    Streams each file once, to EOF, json-parsing only lines that contain a
    wanted id. A call stops being searched once its result is found.
    Returns (uses, results, files_read, bytes_read); a file that fails to
    open or read counts as missing."""
    order: list[str] = []
    for files in plan.values():
        order.extend(f for f in files if f not in order)
    uses: dict = {}
    results: dict = {}
    read: set[str] = set()
    nbytes = 0
    for path in order:
        tids = {t for t, files in plan.items() if path in files and t not in results}
        if not tids:
            continue
        pat = re.compile(b"|".join(re.escape(t.encode()) for t in sorted(tids)))
        try:
            with open(path, "rb") as fh:
                for line in fh:
                    nbytes += len(line)
                    if not pat.search(line):
                        continue
                    if not line.endswith(b"\n"):
                        break  # partial last line: CC may still be appending it
                    try:
                        entry = json.loads(line)
                    except ValueError:  # includes JSONDecodeError / UnicodeDecodeError
                        continue
                    _walk_entry(entry, tids, uses, results)
        except OSError:
            logger.debug("artifact_index: unreadable transcript for %s", sorted(tids),
                         exc_info=True)
            continue
        read.add(path)
    return uses, results, read, nbytes


# ---------------------------------------------------------------------------
# Memo (artifact_calls)
# ---------------------------------------------------------------------------

def _rows(conn, sql: str, ids: list, extra: tuple = ()) -> list:
    out = []
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        out.extend(conn.execute(sql % ",".join("?" * len(chunk)), (*chunk, *extra)).fetchall())
    return out


def _memo_rows(conn, where: str, params=()) -> dict[str, dict]:
    return {
        r[0]: dict(zip(_COLS, r)) for r in
        conn.execute("SELECT %s FROM artifact_calls WHERE %s" % (", ".join(_COLS), where), params)
    }


def _memo_for(conn, tids: list[str]) -> dict[str, dict]:
    return {
        r[0]: dict(zip(_COLS, r)) for r in
        _rows(conn, "SELECT " + ", ".join(_COLS) + " FROM artifact_calls "
              "WHERE tool_use_id IN (%s)", tids)
    }


def _upsert_calls(conn, rows: list[dict]) -> None:
    """Write memo rows, every column listed (raw inserts get no ORM defaults);
    indexed_at keeps its first value on conflict."""
    if rows:
        conn.executemany(_UPSERT_SQL, [tuple(r.get(c) for c in _COLS) for r in rows])


def register_calls(conn, art_rows: list[tuple], now: datetime) -> None:
    """A pending stub per tool_use_id with no memo row yet, committed before
    any transcript I/O: if a later step fails the watermark still moves on,
    and pending rows are retried by every run. The first art_row of an id
    (lowest rowid, i.e. the original rather than a fork) owns the stub."""
    if not art_rows:
        return
    stamp = _db_ts(now)
    stubs, seen = [], set()
    for mid, aid, created, tid in art_rows:
        if tid in seen:
            continue
        seen.add(tid)
        when = _parse_ts(created)
        stubs.append((tid, mid, aid, _db_ts(when) if when else stamp, stamp,
                      "pending", ARTIFACT_EXTRACT_VERSION))
    conn.executemany(
        "INSERT INTO artifact_calls (tool_use_id, message_id, agent_id, created_at, "
        "indexed_at, outcome, extract_version) VALUES (?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(tool_use_id) DO NOTHING",
        stubs,
    )
    conn.commit()


def known_types(conn) -> dict[str, str | None]:
    """type URL → title, from every result seen (any agent or project; type
    URLs are per account). The latest non-null title wins; a publish row's
    own type_name (possibly just the frontmatter "slides") only fills gaps."""
    out: dict[str, str | None] = {}
    fallback: dict[str, str] = {}
    for types_json, type_url, type_name in conn.execute(
        "SELECT types_json, type_url, type_name FROM artifact_calls "
        "WHERE types_json IS NOT NULL OR type_url IS NOT NULL ORDER BY created_at, rowid"
    ):
        pairs = []
        if types_json:
            try:
                pairs = json.loads(types_json)
            except (TypeError, ValueError):
                pairs = []
        for p in pairs if isinstance(pairs, list) else []:
            if isinstance(p, list) and len(p) == 2 and isinstance(p[0], str):
                if p[1] or p[0] not in out:
                    out[p[0]] = p[1] if isinstance(p[1], str) and p[1] else None
        if type_url:
            out.setdefault(type_url, None)
            if type_name:
                fallback[type_url] = type_name
    for u, name in fallback.items():
        out[u] = out[u] or name
    return out


def alias_map(conn) -> tuple[dict[str, str], dict[str, str]]:
    """(alias → key, key → earliest created_at) from memo rows with a key.
    Rows are read oldest first, so an alias shared by two keys keeps the
    older one."""
    amap: dict[str, str] = {}
    first: dict[str, str] = {}
    for key, url, artifact_id, created in conn.execute(
        "SELECT artifact_key, url, artifact_id, created_at FROM artifact_calls "
        "WHERE artifact_key IS NOT NULL ORDER BY created_at, rowid"
    ):
        first.setdefault(key, created)
        for a in _aliases(url, artifact_id) | _aliases(key[len(ARTIFACT_PREFIX):]):
            amap.setdefault(a, key)
    return amap, first


def _lookup(amap: dict, url: str | None) -> str | None:
    if not url:
        return None
    return amap.get(url) or amap.get(uuid_of(url) or "")


def _assign_key(amap: dict, first: dict, c: dict, created: str, tid: str) -> str:
    aliases = _aliases(c["url"], c.get("artifact_id")) | _aliases(c.get("target_url"))
    hits = {amap[a] for a in aliases if a in amap}
    if not hits:
        key = ARTIFACT_PREFIX + c["url"]
    else:
        key = min(hits, key=lambda k: (first.get(k) or "", k))
        if len(hits) > 1:
            logger.warning("artifact_index: call %s matches %d artifact keys; keeping the oldest",
                           tid, len(hits))
    for a in aliases | _aliases(key[len(ARTIFACT_PREFIX):]):
        amap.setdefault(a, key)
    first.setdefault(key, created)
    return key


def _load_agent_rows(conn, ids) -> dict[str, tuple]:
    """id → (project, worktree, session_id, is_subagent, parent_id,
    claude_agent_id), plus every ancestor of a subagent."""
    out: dict[str, tuple] = {}
    want = {i for i in ids if i}
    for _ in range(5):
        want -= set(out)
        if not want:
            break
        for r in _rows(conn, "SELECT id, project, worktree, session_id, is_subagent, parent_id, "
                             "claude_agent_id FROM agents WHERE id IN (%s)", sorted(want)):
            out[r[0]] = tuple(r[1:])
        want = {a[4] for a in out.values() if a[3] and a[4]}
    return out


def resolve_calls(conn, art_rows: list[tuple], *, now: datetime, full: bool = False,
                  interactive: bool = False) -> tuple[list[tuple], int, set[str]]:
    """Classify pending calls from their transcripts and build artifact refs.

    art_rows: (message_id, agent_id, created_at, tool_use_id) scanned this run.
    Work set: every pending memo row, plus on a full run the unavailable ones
    and those extracted by an older ARTIFACT_EXTRACT_VERSION. interactive
    (GET): no transcript I/O at all — refs come from memo rows that are
    already publish, the rest stays pending for the worker.

    Returns (refs, n_pending, fresh_projects): refs are asset_refs tuples,
    one per message row; fresh_projects are the projects of the rows carrying
    a call that became publish in this run, so the caller can pick up prose
    mentions scanned before the key existed."""
    t0 = time.monotonic()
    row_tids = list(dict.fromkeys(r[3] for r in art_rows))
    memo = _memo_for(conn, row_tids) if row_tids else {}
    work: dict[str, dict] = {}
    if not interactive:
        where = "outcome = 'pending'"
        params: tuple = ()
        if full:
            where += " OR outcome = 'unavailable' OR extract_version < ?"
            params = (ARTIFACT_EXTRACT_VERSION,)
        work = _memo_rows(conn, where, params)
        memo.update(work)

    became: dict[str, str] = {}
    copies: dict[str, list[tuple]] = {}
    stats: Counter = Counter()
    files_read: set = set()
    nbytes = 0
    if work:
        for rowid, mid, aid, created, tid in _rows(
            conn, "SELECT rowid, id, agent_id, created_at, tool_use_id FROM messages "
                  "WHERE tool_use_id IN (%s) AND kind = 'tool_use' AND role = 'AGENT' "
                  "ORDER BY rowid", list(work),
        ):
            copies.setdefault(tid, []).append((rowid, mid, aid, created))
        agents = _load_agent_rows(conn, {m["agent_id"] for m in work.values()}
                              | {r[2] for rows in copies.values() for r in rows})
        project_paths = dict(conn.execute("SELECT name, path FROM projects").fetchall())
        transcripts = _Transcripts(conn, agents, project_paths)
        plan: dict[str, list[str]] = {}
        for tid, m in work.items():
            carriers = [m["agent_id"]] + [
                r[2] for r in sorted(copies.get(tid, []), key=lambda r: (str(r[3]), r[0]))]
            files: list[str] = []
            for aid in dict.fromkeys(a for a in carriers if a):
                files.extend(f for f in transcripts.files(aid) if f not in files)
            plan[tid] = files
        uses, results, files_read, nbytes = scan_transcripts(plan)

        types = known_types(conn)
        amap, first = alias_map(conn)
        now_naive = _parse_ts(now)
        writes: list[dict] = []
        for tid in sorted(work, key=lambda t: (str(work[t]["created_at"]), t)):
            m = work[tid]
            old = m["outcome"]
            if tid in results:
                try:
                    c = classify_call(uses.get(tid) or {}, results[tid], types)
                except (TypeError, KeyError, AttributeError, ValueError):
                    logger.debug("artifact_index: bad result shape for %s", tid, exc_info=True)
                    c = {"outcome": "ignored", "reason": "bad_shape", "types": []}
                for u, t in c.get("types") or []:
                    if t or u not in types:
                        types[u] = t
            elif tid in uses:
                created = _parse_ts(m["created_at"])
                age = (now_naive - created).total_seconds() if created and now_naive else None
                if age is not None and age < ARTIFACT_PENDING_TTL_S:
                    continue  # result not written yet: stays pending, retried
                c = {"outcome": "unavailable", "reason": "ttl"}
            else:
                # Every candidate was read to EOF (or none exists): the tool row
                # came from a transcript, so a file without the tool_use will
                # never grow its result. Retried only by full runs.
                c = {"outcome": "unavailable", "reason": "absent"}
            if c["outcome"] == "unavailable" and old != "pending":
                continue  # a re-extraction found nothing: keep what the row knows
            if old == "publish" and c["outcome"] != "publish":
                continue  # never downgrade a publish (its key is permanent)
            key = None
            if c["outcome"] == "publish":
                key = m["artifact_key"] if old == "publish" and m["artifact_key"] else \
                    _assign_key(amap, first, c, str(m["created_at"]), tid)
            elif c["outcome"] == "deleted":
                key = _lookup(amap, c["url"])
                if not key:
                    c = {**c, "outcome": "ignored", "reason": "unknown_key"}
            stats[c["outcome"]] += 1
            if c.get("reason") in ("unconfirmed", "bad_shape", "type_url"):
                stats[c["reason"]] += 1
            m.update({
                "outcome": c["outcome"],
                "action": c.get("action"),
                "artifact_key": key,
                "url": c.get("url"),
                "artifact_id": c.get("artifact_id"),
                "title": c.get("title"),
                "description": c.get("description"),
                "type_url": c.get("type_url"),
                "type_name": c.get("type_name"),
                "types_json": json.dumps(c["types"]) if c.get("types") else None,
                "version": c.get("version"),
                "seq": c.get("seq"),
                "extract_version": ARTIFACT_EXTRACT_VERSION,
            })
            writes.append(m)
            if c["outcome"] == "publish" and old != "publish":
                became[tid] = key
        _upsert_calls(conn, writes)
        conn.commit()  # the memo lands before the refs

    # Refs: one per message row, never per memo row.
    need = {r[1] for r in art_rows} | {r[2] for t in became for r in copies.get(t, [])}
    projects = dict(_rows(conn, "SELECT id, project FROM agents WHERE id IN (%s)",
                          sorted(a for a in need if a)))
    refs: list[tuple] = []
    for mid, aid, created, tid in art_rows:
        m = memo.get(tid)
        if m and m["outcome"] == "publish" and m["artifact_key"] and aid in projects:
            refs.append((projects[aid], m["artifact_key"], "artifact", mid, aid, None, created))
    fresh_projects: set[str] = set()
    for tid, key in became.items():
        for _rowid, mid, aid, created in copies.get(tid, []):
            if aid in projects:
                refs.append((projects[aid], key, "artifact", mid, aid, None, created))
                fresh_projects.add(projects[aid])

    n_pending = conn.execute(
        "SELECT COUNT(*) FROM artifact_calls WHERE outcome = 'pending'").fetchone()[0]
    if stats:
        logger.info(
            "artifact_index: resolved %d call(s) (publish=%d, asset=%d, ignored=%d "
            "[unconfirmed=%d, bad_shape=%d, type_url=%d], error=%d, deleted=%d, "
            "unavailable=%d), pending=%d, read %.1f MiB from %d file(s) in %.2fs",
            sum(stats[k] for k in ("publish", "asset", "ignored", "error", "deleted",
                                    "unavailable")),
            stats["publish"], stats["asset"], stats["ignored"], stats["unconfirmed"],
            stats["bad_shape"], stats["type_url"], stats["error"], stats["deleted"],
            stats["unavailable"], n_pending, nbytes / 1048576, len(files_read),
            time.monotonic() - t0,
        )
    elif work:
        logger.debug("artifact_index: %d call(s) still pending; read %.1f MiB from %d file(s)",
                     n_pending, nbytes / 1048576, len(files_read))
    return refs, n_pending, fresh_projects


# ---------------------------------------------------------------------------
# Prose mentions
# ---------------------------------------------------------------------------

def backfill_mentions(conn, projects, upto_rowid: int) -> list[tuple]:
    """Prose mentions at or below upto_rowid by agents of these projects —
    rows an earlier run scanned before the key they mention existed (a GET
    run that left the call pending, or another agent's prose imported before
    a late subagent tool row, so rowid order alone can't bound it). Walks
    ix_messages_agent_id, so it costs the projects' rows, not the table.
    → [(message_id, agent_id, created_at, url)]."""
    projects = sorted(projects)
    if not projects:
        return []
    out: list[tuple] = []
    for mid, aid, created, content in conn.execute(
        "SELECT id, agent_id, created_at, content FROM messages "
        "WHERE agent_id IN (SELECT id FROM agents WHERE project IN (%s)) "
        "AND rowid <= ? AND role = 'AGENT' "
        "AND (kind IS NULL OR kind = 'text') AND content LIKE '%%claude.ai/%%'"
        % ",".join("?" * len(projects)),
        (*projects, upto_rowid),
    ):
        out.extend((mid, aid, created, u) for u in extract_urls(content))
    return out


def mention_refs(conn, mentions: list[tuple], agents: dict, art_refs: list[tuple]) -> list[tuple]:
    """Prose mentions → "text" refs, only for a key already published in the
    mentioning agent's project. Unknown URLs, type URLs, other people's
    artifacts and cross-project mentions are dropped."""
    if not mentions:
        return []
    amap, _ = alias_map(conn)
    published = {(r[0], r[1]) for r in art_refs}
    published.update(conn.execute(
        "SELECT DISTINCT project, path FROM asset_refs WHERE source = 'artifact'").fetchall())
    missing = sorted({m[1] for m in mentions if m[1] not in agents})
    extra = dict(_rows(conn, "SELECT id, project FROM agents WHERE id IN (%s)", missing)) \
        if missing else {}
    out: list[tuple] = []
    for mid, aid, created, url in mentions:
        key = _lookup(amap, url)
        project = agents[aid][0] if aid in agents else extra.get(aid)
        if key and project and (project, key) in published:
            out.append((project, key, "text", mid, aid, None, created))
    return out


# ---------------------------------------------------------------------------
# Read side
# ---------------------------------------------------------------------------

def minimal_meta(key: str) -> dict:
    """The artifact object built from the key alone (no memo, or a ref-less
    mark in a project that never published it)."""
    return {
        "url": key[len(ARTIFACT_PREFIX):], "kind": "other", "type_name": None,
        "title": None, "description": None, "version": None, "publish_count": 0,
        "first_published_at": None, "last_published_at": None,
        "published_by_agent_id": None, "published_by_agent_name": None,
        "deleted": False,
    }


def key_in_project(conn, key: str, project: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM artifact_calls c JOIN agents a ON a.id = c.agent_id "
        "WHERE c.artifact_key = ? AND a.project = ? LIMIT 1",
        (key, project),
    ).fetchone() is not None


def artifact_meta(conn, keys: list[str]) -> dict[str, dict]:
    """key → artifact object (the API row's "artifact"). Counts are per call
    (memo row): fork copies of a call never count as extra publishes."""
    from asset_index import iso_utc

    by_key: dict[str, list[tuple]] = {}
    for r in _rows(conn, "SELECT artifact_key, outcome, url, title, description, type_url, "
                         "type_name, seq, created_at, agent_id FROM artifact_calls "
                         "WHERE artifact_key IN (%s) ORDER BY created_at, rowid", list(keys)):
        by_key.setdefault(r[0], []).append(r[1:])
    if not by_key:
        return {}
    types = known_types(conn)

    first_pub: dict[str, str] = {}
    for key, rows in by_key.items():
        pubs = [r for r in rows if r[0] == "publish"]
        if pubs and pubs[0][8]:
            first_pub[key] = pubs[0][8]
    owners: dict[str, tuple] = {}
    ag = {r[0]: r[1:] for r in _rows(
        conn, "SELECT id, name, parent_id, is_subagent FROM agents WHERE id IN (%s)",
        sorted(set(first_pub.values())))}
    parents = {r[0]: r[1] for r in _rows(
        conn, "SELECT id, name FROM agents WHERE id IN (%s)",
        sorted({a[1] for a in ag.values() if a[2] and a[1]}))}
    for aid, (name, parent_id, is_sub) in ag.items():
        if is_sub and parent_id and parent_id in parents:
            owners[aid] = (parent_id, parents[parent_id])
        else:
            owners[aid] = (aid, name)

    out: dict[str, dict] = {}
    for key, rows in by_key.items():
        pubs = [r for r in rows if r[0] == "publish"]

        def latest(i, rows=rows):
            return next((r[i] for r in reversed(rows) if r[i]), None)

        type_url = latest(4)
        type_name = (types.get(type_url) or latest(5)) if type_url else None
        seqs = [r[6] for r in rows if isinstance(r[6], int)]
        owner = owners.get(first_pub.get(key), (None, None))
        out[key] = {
            "url": next((r[1] for r in reversed(pubs) if r[1]), None) or key[len(ARTIFACT_PREFIX):],
            "kind": kind_of(type_url, type_name),
            "type_name": type_name,
            "title": latest(2),
            "description": latest(3),
            "version": max(seqs) if seqs else None,
            "publish_count": len(pubs),
            "first_published_at": iso_utc(pubs[0][7]) if pubs else None,
            "last_published_at": iso_utc(pubs[-1][7]) if pubs else None,
            "published_by_agent_id": owner[0],
            "published_by_agent_name": owner[1],
            "deleted": rows[-1][0] == "deleted",
        }
    return out
