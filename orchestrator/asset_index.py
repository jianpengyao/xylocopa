"""Project asset index — the files agents surfaced in chat, per project.

Sources (AGENT messages only, plus the webapps registry):
  text    — path mentioned in agent prose (kind "text" / legacy NULL)
  write   — Write / Edit / MultiEdit / NotebookEdit tool call
  read    — Read tool call (agent looked at it; the "seen" tier)
  send    — SendUserFile tool call
  webapp  — webapp_present card (kind "webapp" message) or WebApp row

Refs live in ``asset_refs`` (derived, rebuildable: bump ASSET_INDEX_VERSION
to force a full rebuild); user state lives in ``asset_marks`` and is never
touched by reindexing. Paths are canonical project-relative posix paths
(realpath'd, so the symlinked xylocopa project and its target share keys).
Paths outside the project (e.g. /tmp scratchpads, other projects) are
dropped — they can't be served anyway.

Indexing is incremental: a rowid watermark over ``messages`` is stored in
SystemConfig[STATE_KEY]. ``catch_up`` is sync; async callers use
``schedule_catch_up`` (coalesced, worker thread, emits ``assets_changed``).
Projects that gain refs (and every project at boot) get their grid-tile
thumbnails pre-rendered by ``schedule_tile_warm``.
"""

import asyncio
import errno
import json
import logging
import os
import posixpath
import re
import selectors
import socket
import sqlite3
import stat as _stat
import threading
import time
from datetime import datetime, timezone
from urllib.parse import quote

logger = logging.getLogger(__name__)

# Bump to wipe asset_refs and rebuild from scratch on the next catch_up
# (detection or resolution rules changed).
ASSET_INDEX_VERSION = 1
STATE_KEY = "asset_index_state"
# Rows below the watermark that are rescanned each run: sync_engine updates
# the newest agent message's content in place (streaming update), so the
# tail must be revisited. INSERT OR IGNORE keeps the rescan idempotent.
OVERLAP = 300

KIND_EXTS = {
    "image": ("png", "jpg", "jpeg", "gif", "svg", "webp"),
    "video": ("mp4", "webm", "mov", "m4v"),
    "html": ("html", "htm"),
    "pdf": ("pdf",),
    "office": ("docx", "doc", "pptx", "ppt", "xlsx", "xls"),
    "3d": ("ply", "obj", "stl", "glb", "gltf", "splat", "spz"),
}
EXT_KIND = {ext: kind for kind, exts in KIND_EXTS.items() for ext in exts}
DELIVERED_SOURCES = frozenset({"text", "write", "send", "webapp"})

_WRITE_TOOLS = frozenset({"Write", "Edit", "MultiEdit", "NotebookEdit"})
_READ_TOOLS = frozenset({"Read"})
_SEND_TOOLS = frozenset({"SendUserFile"})

# ---------------------------------------------------------------------------
# Detection (port of frontend/src/lib/formatters.jsx RE_MD_IMAGE /
# RE_BACKTICK / RE_BARE_PATH, restricted to asset extensions and widened for
# trailing punctuation, quotes, bold and CJK punctuation)
# ---------------------------------------------------------------------------

_EXT_ALT = "|".join(sorted(EXT_KIND, key=len, reverse=True))
# Longest-first so the SQL/py prefilters stay short: ".htm" covers ".html".
_PREFILTER_EXTS = (
    ".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".mp4", ".webm", ".mov",
    ".m4v", ".htm", ".pdf", ".doc", ".ppt", ".xls", ".ply", ".obj", ".stl",
    ".glb", ".gltf", ".splat", ".spz",
)
_CJK_PUNCT = "，。：；！？、（）「」『』【】《》“”‘’"

_RE_MD_IMAGE = re.compile(r"!\[[^\]\n]*\]\((\S+?\.(?:" + _EXT_ALT + r"))\)", re.IGNORECASE)
_RE_BACKTICK = re.compile(r"`([^`\n]*/[^`\n]*\.(?:" + _EXT_ALT + r"))`", re.IGNORECASE)
_RE_BARE_PATH = re.compile(
    # Start of line, or right after whitespace / opening punctuation.
    r"(?:^|(?<=[\s(\[\"'`*<>|,;=" + _CJK_PUNCT + r"]))"
    # First segment (up to the first '/') can't hold '!' (markdown image) or
    # '=' (--out=a/b.png); excluding '/' keeps backtracking linear.
    r"([^/\s()\[\]\"'`*<>|,;=!" + _CJK_PUNCT + r"]*/"
    r"[^\s()\[\]\"'`*<>|,;" + _CJK_PUNCT + r"]+\.(?:" + _EXT_ALT + r"))"
    # End: closing punctuation, a sentence-final period, or end of line.
    r"(?=[\s)\]\"'`*<>|,;:!?" + _CJK_PUNCT + r"]|\.(?![\w/])|$)",
    re.IGNORECASE | re.MULTILINE,
)
_RE_TOOL_SUMMARY = re.compile(r"^> `([^`]+)`(?: (.+))?$")
_RE_API_FILES = re.compile(r"^/?api/files/([^/]+)/(.+)$")

_NOISE_CHARS = frozenset("*?{}<>$")
_NOISE_SUBSTR = (
    "/path/to/", "/.thumbcache/", "/.xylocopa/uploads/", "/.agenthive/uploads/",
    "/node_modules/", "/.git/", "://",
)
_NOISE_SUFFIX = (".thumb.jpg", ".ios.mp4")


def content_may_have_assets(content: str | None) -> bool:
    """Cheap substring prefilter before running the regexes."""
    if not content:
        return False
    lower = content.lower()
    return any(ext in lower for ext in _PREFILTER_EXTS)


def turns_may_have_assets(turns) -> bool:
    """Prefilter for sync_engine new_turns tuples (role, content, meta, ...)."""
    for t in turns:
        if not t or t[0] != "assistant":
            continue
        meta = t[2] if len(t) > 2 else None
        if isinstance(meta, dict) and (meta.get("file_path") or meta.get("files")):
            return True
        if content_may_have_assets(t[1]):
            return True
    return False


def _is_noise(path: str) -> bool:
    if any(c in _NOISE_CHARS for c in path):
        return True
    probe = "/" + path.lower()
    if any(s in probe for s in _NOISE_SUBSTR) or probe.endswith(_NOISE_SUFFIX):
        return True
    if "..." in path or "\u2026" in path:  # elided path (results/.../x.png)
        return True
    if ":" in path.split("/", 1)[0]:  # host:port/x.html, C:/x.png
        return True
    # Bare extension lists like ".png/.jpg/.webp" — no stem in the basename.
    return posixpath.basename(path).rpartition(".")[0] == ""


def _ext_of(path: str) -> str:
    base = posixpath.basename(path)
    _, dot, ext = base.rpartition(".")
    return ext.lower() if dot else ""


def extract_text_paths(content: str | None) -> list[tuple[str, bool]]:
    """Asset path candidates in agent prose → [(raw_path, require_exists)].

    require_exists marks backtick spans containing whitespace: usually a
    shell command (`python x.py --out a/b.png` — the bare pass still finds
    a/b.png), occasionally a real path with a space. Index those only when
    they resolve to an existing file.
    """
    if not content_may_have_assets(content):
        return []
    out: list[tuple[str, bool]] = []
    seen: set[str] = set()

    def add(raw: str, require_exists: bool = False):
        raw = raw.strip()
        if not raw or raw in seen or _is_noise(raw):
            return
        seen.add(raw)
        out.append((raw, require_exists))

    for m in _RE_MD_IMAGE.finditer(content):
        add(m.group(1))
    for m in _RE_BACKTICK.finditer(content):
        span = m.group(1)
        add(span, require_exists=any(c.isspace() for c in span.strip()))
    for m in _RE_BARE_PATH.finditer(content):
        add(m.group(1))
    return out


def extract_message_refs(kind: str | None, content: str | None,
                         meta_raw: str | None) -> list[tuple]:
    """Raw refs for one AGENT message → [(source, raw_path, require_exists, webapp_meta)].

    webapp_meta is the message's metadata.webapp dict for source "webapp"
    (raw_path None), else None.
    """
    if kind == "webapp":
        try:
            wa = (json.loads(meta_raw) or {}).get("webapp") if meta_raw else None
        except (ValueError, TypeError, AttributeError):
            wa = None
        if isinstance(wa, dict) and wa.get("kind") and wa.get("target"):
            return [("webapp", None, False, wa)]
        return []

    if kind == "tool_use":
        m = _RE_TOOL_SUMMARY.match((content or "").strip().split("\n", 1)[0])
        tool = m.group(1) if m else None
        meta = None
        if meta_raw and ('"file_path"' in meta_raw or '"files"' in meta_raw
                         or tool is None):
            try:
                meta = json.loads(meta_raw)
            except (ValueError, TypeError):
                meta = None
        if not isinstance(meta, dict):
            meta = {}
        tool = meta.get("tool_name") or tool
        if tool in _WRITE_TOOLS:
            source = "write"
        elif tool in _READ_TOOLS:
            source = "read"
        elif tool in _SEND_TOOLS:
            source = "send"
        else:
            return []
        refs = []
        # Prefer the full path captured in metadata (jsonl_parser tool_meta);
        # the summary text truncates non-media paths to 2 components, so a
        # summary path is only trusted when it resolves to an existing file.
        fp = meta.get("file_path")
        files = meta.get("files")
        if isinstance(fp, str) and fp:
            refs.append((source, fp, False, None))
        if isinstance(files, list):
            refs.extend((source, f, False, None) for f in files if isinstance(f, str) and f)
        if not refs and m and m.group(2):
            raw = m.group(2).strip()
            refs.append((source, raw, not raw.startswith(("/", "~")), None))
        return [r for r in refs if EXT_KIND.get(_ext_of(r[1])) and not _is_noise(r[1])]

    if kind in (None, "text"):
        return [("text", raw, req, None) for raw, req in extract_text_paths(content)]
    return []


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------

def _keep_rel(rel: str, any_ext: bool = False) -> bool:
    if not rel or rel.startswith("../") or _is_noise(rel):
        return False
    if rel.startswith((".xylocopa/uploads/", ".agenthive/uploads/", ".thumbcache/")):
        return False
    return any_ext or _ext_of(rel) in EXT_KIND


class Resolver:
    """Raw path → canonical project-relative path (or None to drop).

    One instance per indexing run; caches results and project roots.
    """

    def __init__(self, project_paths: dict[str, str]):
        self.project_paths = project_paths
        self._roots: dict[str, str | None] = {}
        self._cache: dict[tuple, str | None] = {}
        self._subdirs: dict[str, list[str]] = {}

    def root(self, project: str) -> str | None:
        if project not in self._roots:
            p = self.project_paths.get(project)
            self._roots[project] = os.path.realpath(p) if p and os.path.isdir(p) else None
        return self._roots[project]

    def resolve(self, project: str, worktree: str | None, raw: str,
                require_exists: bool = False, any_ext: bool = False) -> str | None:
        key = (project, worktree, raw, require_exists, any_ext)
        if key in self._cache:
            return self._cache[key]
        try:
            res = self._resolve(project, worktree, raw, require_exists, any_ext)
        except (OSError, ValueError):
            res = None
        self._cache[key] = res
        return res

    def subdirs(self, root: str) -> list[str]:
        if root not in self._subdirs:
            try:
                # scandir yields listdir order — same pick as files.py
                self._subdirs[root] = [e.path for e in os.scandir(root) if e.is_dir()]
            except OSError:
                self._subdirs[root] = []
        return self._subdirs[root]

    def _canon(self, root: str, abs_path: str, any_ext: bool) -> str | None:
        real = os.path.realpath(abs_path)
        if not real.startswith(root + "/"):
            return None
        rel = real[len(root) + 1:]
        return rel if _keep_rel(rel, any_ext) else None

    def _resolve(self, project, worktree, raw, require_exists, any_ext):
        root = self.root(project)
        if root is None:
            return None
        p = raw.strip().strip("\"'`*").strip()
        if not p or _is_noise(p):
            return None
        p = os.path.expanduser(p)
        p = p.replace("/agenthive-projects/", "/xylocopa-projects/")
        m = _RE_API_FILES.match(p)
        if m:
            if m.group(1) != project:
                return None
            p = m.group(2)
        elif p.startswith("/projects/") and not os.path.isdir("/projects"):
            # Docker-era layout: /projects/<name>/<rest>
            parts = p.split("/", 3)
            if len(parts) < 4 or parts[2] != project:
                return None
            p = parts[3]

        if p.startswith("/"):
            if require_exists and not os.path.exists(p):
                return None
            return self._canon(root, p, any_ext)

        while p.startswith("./"):
            p = p[2:]
        rels = [p]
        if p.startswith(project + "/"):
            # "<project>/x.png" usually means <root>/x.png, but a project may
            # genuinely contain a same-named subdir — try both.
            rels = [p[len(project) + 1:], p]
        bases = []
        if worktree:
            bases.append(os.path.join(root, ".claude", "worktrees", worktree))
        bases.append(root)
        for rel in rels:
            for base in bases:
                cand = os.path.join(base, rel)
                if os.path.exists(cand):
                    return self._canon(root, cand, any_ext)
        # files._resolve_project_file "Fallback 2" also probes one directory
        # level down, so the chat previews these — index them the same way.
        for sub in self.subdirs(root):
            cand = os.path.join(sub, rels[0])
            if os.path.isfile(cand):
                res = self._canon(root, cand, any_ext)
                if res:
                    return res
        if require_exists:
            return None
        head = rels[0].split("/", 1)[0]
        if head != project and head in self.project_paths:
            return None  # "<other-project>/x.png" — not this project's file
        return self._canon(root, os.path.join(root, rels[0]), any_ext)

    def webapp_path(self, project: str, kind: str, target: str) -> str | None:
        target = (target or "").strip()
        if not target:
            return None
        if kind == "port":
            return f"webapp:port:{target}" if target.isdigit() else None
        if kind == "url":
            return f"webapp:url:{target}"
        if kind != "static":
            return None
        root = self.root(project)
        if root is None:
            return None
        if target.startswith("/"):
            return self.resolve(project, None, target, any_ext=True)
        try:
            return self._canon(root, os.path.join(root, target), any_ext=True)
        except (OSError, ValueError):
            return None


# ---------------------------------------------------------------------------
# Incremental indexing
# ---------------------------------------------------------------------------

_index_lock = threading.Lock()

_INSERT_SQL = (
    "INSERT OR IGNORE INTO asset_refs "
    "(project, path, source, message_id, agent_id, webapp_id, created_at) "
    "VALUES (?, ?, ?, ?, ?, ?, ?)"
)


def _dbapi(db):
    return db.connection().connection.dbapi_connection


def _load_state(conn) -> dict:
    row = conn.execute(
        "SELECT value FROM system_config WHERE key = ?", (STATE_KEY,)
    ).fetchone()
    if not row:
        return {}
    try:
        state = json.loads(row[0])
    except (ValueError, TypeError):
        return {}
    return state if isinstance(state, dict) else {}


def _save_state(conn, state: dict) -> None:
    conn.execute(
        "INSERT INTO system_config (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (STATE_KEY, json.dumps(state)),
    )


def _candidate_sql() -> str:
    likes = " OR ".join(f"content LIKE '%{e}%'" for e in _PREFILTER_EXTS)
    return (
        "SELECT rowid, id, agent_id, kind, content, metadata, created_at "
        "FROM messages WHERE rowid > ? AND role = 'AGENT' AND ("
        "kind = 'webapp' OR metadata LIKE '%\"file_path\"%' "
        "OR metadata LIKE '%\"files\"%' OR " + likes + ") ORDER BY rowid"
    )


_CANDIDATE_SQL = _candidate_sql()


def _load_agents(conn, ids) -> dict[str, tuple[str, str | None]]:
    out: dict[str, tuple[str, str | None]] = {}
    ids = list(ids)
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        q = "SELECT id, project, worktree FROM agents WHERE id IN (%s)" % ",".join("?" * len(chunk))
        for aid, proj, wt in conn.execute(q, chunk):
            out[aid] = (proj, wt)
    return out


def _insert_refs(conn, refs: list[tuple]) -> set[str]:
    """INSERT OR IGNORE grouped by project; returns projects that gained rows."""
    changed: set[str] = set()
    by_project: dict[str, list[tuple]] = {}
    for r in refs:
        by_project.setdefault(r[0], []).append(r)
    for project, rows in by_project.items():
        before = conn.total_changes
        try:
            conn.executemany(_INSERT_SQL, rows)
        except sqlite3.IntegrityError:
            # OR IGNORE doesn't cover FK errors: the project was deleted or
            # renamed mid-run. Skip it rather than wedge every later run.
            conn.rollback()
            logger.warning("asset_index: dropped %d ref(s) for vanished project %r",
                           len(rows), project)
            continue
        if conn.total_changes > before:
            changed.add(project)
        conn.commit()  # per project: keep the write lock short on a full run
    return changed


def _sync_webapps(conn, resolver: Resolver, projects: set[str]) -> set[str]:
    """Mirror WebApp registry rows as message-less "webapp" refs.

    NULLs are distinct in the unique constraint, so these rows are diffed
    by webapp_id here instead of relying on INSERT OR IGNORE.
    """
    apps = conn.execute(
        "SELECT id, project, kind, target, created_by_agent, created_at, "
        "last_presented_at FROM webapps"
    ).fetchall()
    existing: dict[str, list[tuple]] = {}
    for rid, proj, path, wid, aid, cat in conn.execute(
        "SELECT id, project, path, webapp_id, agent_id, created_at FROM asset_refs "
        "WHERE source = 'webapp' AND message_id IS NULL"
    ):
        existing.setdefault(wid, []).append((rid, proj, path, aid, cat))

    app_ids = {a[0] for a in apps}
    deletes: list[int] = []
    inserts: list[tuple] = []
    updates: list[tuple] = []
    for wid, rows in existing.items():
        if wid not in app_ids:
            deletes.extend(r[0] for r in rows)
    for wid, proj, kind, target, creator, cat, lpa in apps:
        if proj not in projects:
            continue  # stale project name (rename gap) — leave refs alone
        path = resolver.webapp_path(proj, kind, target)
        rows = existing.get(wid, [])
        when = lpa or cat
        if path is None:
            deletes.extend(r[0] for r in rows)
            continue
        same = [r for r in rows if r[1] == proj and r[2] == path]
        deletes.extend(r[0] for r in rows if r not in same[:1])
        if same:
            r = same[0]
            if r[3] != creator or r[4] != when:
                updates.append((creator, when, r[0]))
        else:
            inserts.append((proj, path, "webapp", None, creator, wid, when))

    for i in range(0, len(deletes), 500):
        chunk = deletes[i:i + 500]
        conn.execute("DELETE FROM asset_refs WHERE id IN (%s)" % ",".join("?" * len(chunk)), chunk)
    if updates:
        conn.executemany("UPDATE asset_refs SET agent_id = ?, created_at = ? WHERE id = ?", updates)
    changed: set[str] = set()
    if inserts:
        conn.executemany(_INSERT_SQL, inserts)
        changed = {r[0] for r in inserts}
    if deletes or updates or inserts:
        conn.commit()
    return changed


def _webapps_fingerprint(conn) -> list:
    row = conn.execute(
        "SELECT COUNT(*), MAX(created_at), MAX(last_presented_at) FROM webapps"
    ).fetchone()
    pcount = conn.execute("SELECT COUNT(*) FROM projects").fetchone()[0]
    return [row[0], row[1], row[2], pcount]


def catch_up(db=None) -> set[str]:
    """Index messages newer than the watermark; returns projects with new refs."""
    own = db is None
    if own:
        from database import SessionLocal
        db = SessionLocal()
    try:
        with _index_lock:
            try:
                return _catch_up_locked(db)
            except Exception:
                db.rollback()  # drop a half-written batch before db is reused
                raise
    finally:
        if own:
            db.close()


def _catch_up_locked(db) -> set[str]:
    # Raw DBAPI connection throughout, committed directly: Session.commit()
    # would release the connection (NullPool closes it) mid-run.
    t0 = time.monotonic()
    conn = _dbapi(db)
    state = _load_state(conn)
    if state.get("version") != ASSET_INDEX_VERSION:
        # Refs whose message was purged can't be regenerated — keep them (the
        # asset must survive); everything else is rebuilt below.
        conn.execute(
            "DELETE FROM asset_refs WHERE message_id IS NULL "
            "OR message_id IN (SELECT id FROM messages)"
        )
        conn.commit()
        state = {"version": ASSET_INDEX_VERSION, "rowid": 0}
    w = int(state.get("rowid") or 0)
    max_rowid = conn.execute("SELECT COALESCE(MAX(rowid), 0) FROM messages").fetchone()[0]
    if max_rowid < w:
        # Rowids went backwards (newest rows deleted, or VACUUM renumbered).
        w = 0
    start = max(0, w - OVERLAP)
    full = start == 0

    project_paths = dict(conn.execute("SELECT name, path FROM projects").fetchall())
    resolver = Resolver(project_paths)
    webapp_ids = {
        (p, k, t): wid for wid, p, k, t in
        conn.execute("SELECT id, project, kind, target FROM webapps")
    }

    # Phase 1: stream candidate rows, extract raw refs (no resolution yet,
    # so the read cursor is drained before any write).
    pending: list[tuple] = []  # (msg_id, agent_id, created_at, raw_refs)
    agent_ids: set[str] = set()
    scanned = 0
    cur = conn.execute(_CANDIDATE_SQL, (start,))
    while True:
        rows = cur.fetchmany(2000)
        if not rows:
            break
        scanned += len(rows)
        for _rowid, mid, aid, kind, content, meta_raw, created in rows:
            try:
                raw_refs = extract_message_refs(kind, content, meta_raw)
            except Exception:  # noqa: BLE001 — one bad row must not stall the index
                logger.debug("asset_index: extract failed for %s", mid, exc_info=True)
                continue
            if raw_refs:
                pending.append((mid, aid, created, raw_refs))
                agent_ids.add(aid)
    cur.close()

    # Phase 2: resolve against each agent's project (+ worktree).
    agents = _load_agents(conn, agent_ids)
    refs: list[tuple] = []
    for mid, aid, created, raw_refs in pending:
        ag = agents.get(aid)
        if not ag:
            continue
        a_project, a_worktree = ag
        for source, raw, req, wa in raw_refs:
            try:
                if source == "webapp":
                    proj = wa.get("project") or a_project
                    if proj not in project_paths:
                        proj = a_project
                    if proj not in project_paths:
                        continue  # orphan agent — asset_refs.project is a real FK
                    path = resolver.webapp_path(proj, wa.get("kind"), wa.get("target"))
                    if path:
                        wid = webapp_ids.get((proj, wa.get("kind"), wa.get("target")))
                        refs.append((proj, path, source, mid, aid, wid, created))
                    continue
                path = resolver.resolve(a_project, a_worktree, raw, req)
                if path:
                    refs.append((a_project, path, source, mid, aid, None, created))
            except Exception:  # noqa: BLE001 — one bad ref must not stall the index
                logger.debug("asset_index: resolve failed for %s", mid, exc_info=True)

    changed = _insert_refs(conn, refs)

    fp = _webapps_fingerprint(conn)
    if full or state.get("webapps") != fp:
        try:
            changed |= _sync_webapps(conn, resolver, set(project_paths))
            state["webapps"] = fp
        except sqlite3.IntegrityError:
            conn.rollback()  # project vanished mid-run; retried next run
            logger.warning("asset_index: webapp registry sync failed", exc_info=True)

    if max_rowid != state.get("rowid") or full:
        state["rowid"] = max_rowid
        state["version"] = ASSET_INDEX_VERSION
        _save_state(conn, state)
        conn.commit()
    db.commit()  # end the Session's transaction (no-op at the DBAPI level)
    (logger.info if full or changed else logger.debug)(
        "asset_index: scanned %d rows (from rowid %d) → %d refs, %d project(s) gained refs in %.2fs",
        scanned, start, len(refs), len(changed), time.monotonic() - t0,
    )
    return changed


# ---------------------------------------------------------------------------
# Async trigger (coalesced) — used by sync_engine / webapp hooks
# ---------------------------------------------------------------------------

_main_event_loop: asyncio.AbstractEventLoop | None = None
_sched_lock = threading.Lock()
_sched_running = False
_sched_dirty = False


def schedule_catch_up() -> None:
    """Fire-and-forget incremental index; safe from any thread, never raises.

    A burst of calls while a run is in flight collapses into one rerun.
    Projects that gained refs get a WS ``assets_changed`` event.
    """
    global _sched_running, _sched_dirty, _main_event_loop
    try:
        try:
            _main_event_loop = asyncio.get_running_loop()
        except RuntimeError:
            pass  # worker thread: keep the loop main.py injected
        with _sched_lock:
            if _sched_running:
                _sched_dirty = True
                return
            _sched_running = True
        threading.Thread(target=_sched_worker, name="asset-index", daemon=True).start()
    except Exception:  # noqa: BLE001 — hooks must never fail because of the index
        logger.debug("asset_index: schedule failed", exc_info=True)
        with _sched_lock:
            _sched_running = False


def _sched_worker() -> None:
    global _sched_running, _sched_dirty
    try:
        while True:
            with _sched_lock:
                _sched_dirty = False
            try:
                changed = catch_up()
            except Exception:  # noqa: BLE001
                logger.warning("asset_index: catch_up failed", exc_info=True)
                changed = set()
            if changed:
                _emit_changed(changed)
                schedule_tile_warm(changed)
            with _sched_lock:
                if not _sched_dirty:
                    _sched_running = False
                    return
    except BaseException:
        with _sched_lock:
            _sched_running = False
        raise


def _emit_changed(projects: set[str]) -> None:
    loop = _main_event_loop
    if not loop or not loop.is_running():
        return
    from websocket import emit_assets_changed
    for p in sorted(projects):
        asyncio.run_coroutine_threadsafe(emit_assets_changed(p), loop)


def startup_catch_up() -> None:
    """Boot-time catch-up (background thread). Emits nothing — no clients
    care at boot, and the GET endpoint catches up on its own. Then warms
    every project's tile thumbnails."""
    try:
        catch_up()
    except Exception:  # noqa: BLE001
        logger.warning("asset_index: startup catch_up failed", exc_info=True)
    schedule_tile_warm()


# ---------------------------------------------------------------------------
# Tile thumbnail warm-up
# ---------------------------------------------------------------------------
# A tile's first view otherwise waits on Pillow / ffmpeg (~0.2-0.5 s for a
# big image or video, and a screenful of them queue up). One background
# thread renders the delivered tier's tiles ahead of time, newest first;
# the thumbs route still renders whatever isn't warm yet.

_TILE_EXTS = frozenset({"png", "jpg", "jpeg", "gif", "webp", "mp4", "mov", "webm", "m4v"})
_WARM_ALL = "\0all"
_warm_lock = threading.Lock()
_warm_running = False
_warm_queue: set[str] = set()


def schedule_tile_warm(projects=None) -> None:
    """Fire-and-forget tile warm-up for ``projects`` (None = all). Requests
    made while a run is in flight are queued for one follow-up run. Never
    raises."""
    global _warm_running
    try:
        with _warm_lock:
            _warm_queue.update(projects if projects is not None else (_WARM_ALL,))
            if _warm_running:
                return
            _warm_running = True
        threading.Thread(target=_warm_worker, name="asset-tile-warm", daemon=True).start()
    except Exception:  # noqa: BLE001
        logger.debug("asset_index: tile warm schedule failed", exc_info=True)
        with _warm_lock:
            _warm_running = False


def _warm_worker() -> None:
    global _warm_running
    try:
        while True:
            with _warm_lock:
                if not _warm_queue:
                    _warm_running = False
                    return
                batch = set(_warm_queue)
                _warm_queue.clear()
            try:
                n = warm_tiles(None if _WARM_ALL in batch else batch)
                if n:
                    logger.info("asset_index: pre-rendered %d tile thumbnails", n)
            except Exception:  # noqa: BLE001
                logger.warning("asset_index: tile warm-up failed", exc_info=True)
    except BaseException:
        with _warm_lock:
            _warm_running = False
        raise


def warm_tiles(projects=None, db=None) -> int:
    """Render missing / stale tile thumbnails for delivered image and video
    assets of ``projects`` (None = all), newest first. Returns the number
    written."""
    from routers.files import warm_tile_thumb
    own = db is None
    if own:
        from database import SessionLocal
        db = SessionLocal()
    work: list[tuple[str, str, str]] = []  # (last ref, root, rel path)
    try:
        conn = _dbapi(db)
        sources = sorted(DELIVERED_SOURCES)
        for name, path in conn.execute("SELECT name, path FROM projects").fetchall():
            if projects is not None and name not in projects:
                continue
            if not path or not os.path.isdir(path):
                continue
            root = os.path.realpath(path)
            for rel, last in conn.execute(
                "SELECT path, MAX(created_at) FROM asset_refs WHERE project = ? "
                "AND source IN (%s) GROUP BY path" % ",".join("?" * len(sources)),
                (name, *sources),
            ):
                if _ext_of(rel) in _TILE_EXTS:
                    work.append((str(last or ""), root, rel))
    finally:
        if own:
            db.close()  # don't hold a connection through minutes of rendering
    work.sort(reverse=True)
    done = 0
    for _, root, rel in work:
        # realpath: the thumbs route caches under the resolved file's dir
        full = os.path.realpath(os.path.join(root, rel))
        if not full.startswith(root + os.sep) or not os.path.isfile(full):
            continue
        try:
            if warm_tile_thumb(full):
                done += 1
        except Exception:  # noqa: BLE001 — undecodable file: skip it
            logger.debug("asset_index: tile warm failed for %s", full, exc_info=True)
    return done


# ---------------------------------------------------------------------------
# Read side
# ---------------------------------------------------------------------------

def iso_utc(value) -> str | None:
    """SQLite naive-UTC datetime (str or datetime) → ISO 8601 with 'Z',
    formatted like the pydantic schemas (microseconds only when non-zero)."""
    if value is None:
        return None
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return value
    if value.tzinfo is not None:
        value = value.astimezone(timezone.utc).replace(tzinfo=None)
    return value.isoformat() + "Z"


def _probe_ports(ports: list[int], timeout: float = 0.1) -> dict[int, bool]:
    """Concurrent TCP connect to 127.0.0.1:<port>; True if something listens."""
    result: dict[int, bool] = {}
    sel = selectors.DefaultSelector()
    try:
        for port in ports:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.setblocking(False)
            rc = s.connect_ex(("127.0.0.1", port))
            if rc == 0:
                result[port] = True
                s.close()
            elif rc in (errno.EINPROGRESS, errno.EAGAIN, errno.EWOULDBLOCK):
                sel.register(s, selectors.EVENT_WRITE, port)
            else:
                result[port] = False
                s.close()
        deadline = time.monotonic() + timeout
        while sel.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            for key, _ in sel.select(remaining):
                s = key.fileobj
                err = s.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
                result[key.data] = err == 0
                sel.unregister(s)
                s.close()
        for key in list(sel.get_map().values()):
            result.setdefault(key.data, False)
            sel.unregister(key.fileobj)
            key.fileobj.close()
    finally:
        sel.close()
    return result


def _new_agg(created) -> dict:
    return {
        "sources": set(), "first": created, "last": created,
        "mids": set(), "nrefs": 0, "best": None, "best_key": None,
        "webapp_id": None, "webapp_when": None, "app": None,
    }


def _mark_path_ok(root: str | None, path: str) -> bool:
    """Marks accept arbitrary client paths: a ref-less mark is listed only
    when its path is a well-formed asset key inside the project."""
    if path.startswith("webapp:port:"):
        return path[len("webapp:port:"):].isdigit()
    if path.startswith("webapp:url:"):
        return path[len("webapp:url:"):].startswith(("http://", "https://"))
    if not root or path.startswith("/") or not _keep_rel(path):
        return False
    return os.path.realpath(os.path.join(root, path)).startswith(root + "/")


def _card_titles(conn, mids: list[str]) -> dict[str, str]:
    """message id → metadata.webapp.title for webapp card messages."""
    out: dict[str, str] = {}
    for i in range(0, len(mids), 500):
        chunk = mids[i:i + 500]
        for mid, meta_raw in conn.execute(
            "SELECT id, metadata FROM messages WHERE id IN (%s)" % ",".join("?" * len(chunk)),
            chunk,
        ):
            try:
                wa = (json.loads(meta_raw) or {}).get("webapp") if meta_raw else None
            except (ValueError, TypeError, AttributeError):
                wa = None
            if isinstance(wa, dict) and isinstance(wa.get("title"), str):
                out[mid] = wa["title"]
    return out


def list_assets(db, project_name: str, include_missing: bool = False) -> dict:
    """Aggregate refs per path for one project, joined with marks + disk state."""
    conn = _dbapi(db)
    row = conn.execute("SELECT path FROM projects WHERE name = ?", (project_name,)).fetchone()
    root = None
    if row and row[0] and os.path.isdir(row[0]):
        root = os.path.realpath(row[0])

    agg: dict[str, dict] = {}
    for (path, source, mid, aid, wid, created, msg_ok, agent_ok,
         parent_id, is_sub, agent_name) in conn.execute(
        "SELECT r.path, r.source, r.message_id, r.agent_id, r.webapp_id, r.created_at, "
        "m.id IS NOT NULL, a.id IS NOT NULL, a.parent_id, a.is_subagent, a.name "
        "FROM asset_refs r "
        "LEFT JOIN messages m ON m.id = r.message_id "
        "LEFT JOIN agents a ON a.id = r.agent_id "
        "WHERE r.project = ?",
        (project_name,),
    ):
        a = agg.get(path)
        if a is None:
            a = agg[path] = _new_agg(created)
        a["sources"].add(source)
        a["nrefs"] += 1
        if mid:
            a["mids"].add(mid)
        if created and (a["first"] is None or created < a["first"]):
            a["first"] = created
        if created and (a["last"] is None or created > a["last"]):
            a["last"] = created
        if wid and (a["webapp_when"] is None or (created or "") >= a["webapp_when"]):
            a["webapp_id"], a["webapp_when"] = wid, created or ""
        top = bool(agent_ok) and not parent_id and not is_sub
        # Deep-link target: a live top-level message first, then the bubble
        # that showed the file to the user over a later agent-only Read.
        key = (bool(msg_ok) and top, bool(msg_ok) and bool(agent_ok), bool(msg_ok),
               source in DELIVERED_SOURCES, created or "")
        if a["best_key"] is None or key > a["best_key"]:
            a["best_key"] = key
            a["best"] = (aid if agent_ok else None,
                         agent_name if agent_ok else None,
                         mid if msg_ok else None)

    marks: dict[str, tuple] = {}
    for p, s, h, n, upd in conn.execute(
        "SELECT path, starred, hidden, note, updated_at FROM asset_marks WHERE project = ?",
        (project_name,),
    ):
        marks[p] = (bool(s), bool(h), n)
        # A mark whose path lost every ref (purged messages + an index
        # rebuild) is still the user's curation — list it with sources=[].
        if p not in agg and _mark_path_ok(root, p):
            agg[p] = _new_agg(upd)

    secret = conn.execute(
        "SELECT value FROM system_config WHERE key = 'jwt_secret'"
    ).fetchone()

    def app_obj(wid, wproj, kind, target, title):
        app = {"id": wid, "kind": kind, "target": target, "title": title or ""}
        # Same src the chat card carries (mcp_server webapp_present): the
        # signed port-proxy prefix, or the url itself. The proxy only serves
        # registered ports, so a port src needs a registry row.
        if kind == "port" and wid and secret and str(target).isdigit():
            from routers.preview import port_proxy_sig
            sig = port_proxy_sig(secret[0], wproj, int(target))
            app["src"] = f"/api/preview/p/{sig}/{quote(wproj)}/{target}/"
        elif kind == "url":
            app["src"] = target
        return app

    wids = sorted({a["webapp_id"] for a in agg.values() if a["webapp_id"]})
    webapps: dict[str, dict] = {}
    for i in range(0, len(wids), 500):
        chunk = wids[i:i + 500]
        for wid, wproj, kind, target, title in conn.execute(
            "SELECT id, project, kind, target, title FROM webapps WHERE id IN (%s)"
            % ",".join("?" * len(chunk)),
            chunk,
        ):
            webapps[wid] = app_obj(wid, wproj, kind, target, title)
    # port / url keys with no linked registry row: match the registry by
    # target, else build the object from the key + the card's title, so the
    # client never mistakes them for project files.
    unlinked = [p for p, a in agg.items()
                if p.startswith(("webapp:port:", "webapp:url:"))
                and webapps.get(a["webapp_id"]) is None]
    if unlinked:
        by_key = {
            f"webapp:{kind}:{target}": app_obj(wid, project_name, kind, target, title)
            for wid, kind, target, title in conn.execute(
                "SELECT id, kind, target, title FROM webapps "
                "WHERE project = ? AND kind IN ('port', 'url')",
                (project_name,),
            )
        }
        titles = _card_titles(conn, [agg[p]["best"][2] for p in unlinked
                                     if agg[p]["best"] and agg[p]["best"][2]])
        for p in unlinked:
            a = agg[p]
            a["app"] = by_key.get(p)
            if a["app"] is None:
                _, kind, target = p.split(":", 2)
                a["app"] = app_obj(None, project_name, kind, target,
                                   titles.get((a["best"] or (None, None, None))[2]))

    port_paths = sorted(
        (p for p in agg if p.startswith("webapp:port:")),
        key=lambda p: agg[p]["last"] or "", reverse=True,
    )
    ports = []
    for p in port_paths[:100]:
        try:
            ports.append(int(p.rsplit(":", 1)[1]))
        except ValueError:
            pass
    port_alive = _probe_ports(ports) if ports else {}

    assets = []
    for path, a in agg.items():
        starred, hidden, note = marks.get(path, (False, False, None))
        webapp = a["app"] or (webapps.get(a["webapp_id"]) if a["webapp_id"] else None)
        size = mtime = None
        if path.startswith("webapp:port:"):
            target = path[len("webapp:port:"):]
            exists = port_alive.get(int(target), False) if target.isdigit() else False
            name, dirname, ext = (webapp or {}).get("title") or f"localhost:{target}", "", ""
        elif path.startswith("webapp:url:"):
            target = path[len("webapp:url:"):]
            exists = True
            name, dirname, ext = (webapp or {}).get("title") or target, "", ""
        else:
            name, dirname, ext = posixpath.basename(path), posixpath.dirname(path), _ext_of(path)
            exists = False
            if root:
                try:
                    st = os.stat(os.path.join(root, path))
                    if _stat.S_ISREG(st.st_mode):
                        exists, size, mtime = True, st.st_size, st.st_mtime
                except OSError:
                    pass
        kind = ("webapp" if "webapp" in a["sources"] or path.startswith("webapp:")
                else EXT_KIND.get(ext))
        if kind is None:
            continue
        if not exists and not include_missing and not starred:
            continue
        last_agent_id, last_agent_name, last_mid = a["best"] or (None, None, None)
        assets.append({
            "path": path,
            "name": name,
            "dir": dirname,
            "kind": kind,
            "ext": ext,
            "sources": sorted(a["sources"]),
            # Ref-less marks count as delivered: the user curated them.
            "tier": ("delivered" if a["sources"] & DELIVERED_SOURCES or not a["sources"]
                     else "seen"),
            "first_seen_at": iso_utc(a["first"]),
            "last_seen_at": iso_utc(a["last"]),
            "mention_count": len(a["mids"]) or a["nrefs"],
            "last_agent_id": last_agent_id,
            "last_agent_name": last_agent_name,
            "last_message_id": last_mid,
            "exists": exists,
            "size": size,
            "mtime": mtime,
            "starred": starred,
            "hidden": hidden,
            "note": note,
            "webapp": webapp,
        })
    assets.sort(key=lambda x: x["last_seen_at"] or "", reverse=True)
    return {"assets": assets}


# Serializes set_marks' read-modify-write: two overlapping POSTs for the same
# unmarked path would otherwise both INSERT (UNIQUE error → 500).
_marks_lock = threading.Lock()


def set_marks(db, project_name: str, paths: list[str], starred: bool | None = None,
              hidden: bool | None = None, note: str | None = None) -> dict[str, dict]:
    """Batch-update marks (None = unchanged; note "" clears). A mark that
    returns to all-default is deleted."""
    with _marks_lock:
        return _set_marks_locked(db, project_name, paths, starred, hidden, note)


def _set_marks_locked(db, project_name, paths, starred, hidden, note) -> dict[str, dict]:
    from models import AssetMark
    from utils import utcnow

    uniq = list(dict.fromkeys(p for p in paths if p))
    existing: dict[str, AssetMark] = {}
    for i in range(0, len(uniq), 500):
        chunk = uniq[i:i + 500]
        for m in db.query(AssetMark).filter(
            AssetMark.project == project_name, AssetMark.path.in_(chunk),
        ):
            existing[m.path] = m
    set_note = note is not None
    if set_note:
        note = note.strip() or None

    now = utcnow()
    out: dict[str, dict] = {}
    for p in uniq:
        m = existing.get(p)
        s = m.starred if m else False
        h = m.hidden if m else False
        n = m.note if m else None
        if starred is not None:
            s = bool(starred)
        if hidden is not None:
            h = bool(hidden)
        if set_note:
            n = note
        if not s and not h and n is None:
            if m is not None:
                db.delete(m)
        elif m is None:
            db.add(AssetMark(project=project_name, path=p, starred=s, hidden=h,
                             note=n, updated_at=now))
        else:
            m.starred, m.hidden, m.note, m.updated_at = s, h, n, now
        out[p] = {"starred": s, "hidden": h, "note": n}
    db.commit()
    return out
