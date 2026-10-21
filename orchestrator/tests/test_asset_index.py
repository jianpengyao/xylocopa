"""Tests for the project Assets index (asset_index.py + routers/assets.py)."""

import json
import os
import socket
from datetime import datetime, timedelta

import pytest

import asset_index as ai
from models import (
    Agent, AgentMode, AgentStatus, AssetMark, AssetRef, Message, MessageRole,
    MessageStatus, Project, SystemConfig, WebApp,
)

PNG_1PX = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06"
    b"\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\rIDATx\x9cc\xf8\x0f\x00\x00\x01\x01"
    b"\x00\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82"
)
T0 = datetime(2026, 10, 1, 12, 0, 0)


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

def _touch(path, data=PNG_1PX):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)


_real_schedule_tile_warm = ai.schedule_tile_warm


@pytest.fixture(autouse=True)
def _no_background_tile_warm(monkeypatch):
    """GET /assets and the catch-up worker start a tile warm-up thread on the
    global SessionLocal, not the test engine — keep it out of these tests."""
    monkeypatch.setattr(ai, "schedule_tile_warm", lambda projects=None: None)


@pytest.fixture()
def env(tmp_path, db_session):
    """Project 'demo' on disk (+ worktree wt1) and a sibling project 'other'."""
    base = tmp_path / "xylocopa-projects"
    root = base / "demo"
    _touch(str(root / "out" / "plot.png"))
    _touch(str(root / "out" / "seen.png"))
    _touch(str(root / "docs" / "report.pdf"), b"%PDF-1.4")
    _touch(str(root / "webapps" / "viewer" / "index.html"), b"<html></html>")
    _touch(str(root / ".claude" / "worktrees" / "wt1" / "out" / "wt_only.png"))
    _touch(str(root / ".claude" / "worktrees" / "wt1" / "out" / "plot.png"))
    other = base / "other"
    _touch(str(other / "x.png"))
    _touch(str(tmp_path / "scratch" / "tmp.png"))

    db_session.add_all([
        Project(name="demo", display_name="Demo", path=str(root)),
        Project(name="other", display_name="Other", path=str(other)),
    ])
    db_session.commit()
    db_session.add_all([
        Agent(id="agentmain001", project="demo", name="Main agent",
              mode=AgentMode.AUTO, status=AgentStatus.STOPPED),
        Agent(id="agentwt00001", project="demo", name="Worktree agent",
              mode=AgentMode.AUTO, status=AgentStatus.STOPPED, worktree="wt1"),
    ])
    db_session.commit()
    db_session.add(Agent(id="agentsub0001", project="demo", name="Subagent",
                         mode=AgentMode.AUTO, status=AgentStatus.STOPPED,
                         is_subagent=True, parent_id="agentmain001"))
    db_session.commit()
    return {"tmp": tmp_path, "root": root, "other": other}


def _msg(db, content, agent_id="agentmain001", kind="text", meta=None,
         role=MessageRole.AGENT, minutes=0):
    m = Message(
        agent_id=agent_id, role=role, content=content, kind=kind,
        meta_json=json.dumps(meta) if meta is not None else None,
        status=MessageStatus.COMPLETED, created_at=T0 + timedelta(minutes=minutes),
    )
    db.add(m)
    db.commit()
    return m


def _refs(db, project="demo"):
    return sorted(
        (r.path, r.source, r.message_id) for r in
        db.query(AssetRef).filter(AssetRef.project == project).all()
    )


def _by_path(result):
    return {a["path"]: a for a in result["assets"]}


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text,expected", [
    ("saved to out/plot.png.", ["out/plot.png"]),
    ("out/a.png, out/b.png; out/c.png: done", ["out/a.png", "out/b.png", "out/c.png"]),
    ('see "out/a.png" and \'out/b.pdf\'', ["out/a.png", "out/b.pdf"]),
    ("**out/a.png**", ["out/a.png"]),
    ("`out/a.mp4`", ["out/a.mp4"]),
    ("![chart](plot.png)", ["plot.png"]),
    ("[report](docs/report.pdf)", ["docs/report.pdf"]),
    ('<img src="docs/hero.svg">', ["docs/hero.svg"]),
    ("--out=results/a.png", ["results/a.png"]),
    ("path: /abs/x.mp4;", ["/abs/x.mp4"]),
    ("保存到 out/a.png。路径：out/b.pdf，好", ["out/a.png", "out/b.pdf"]),
    ("viewer at webapps/v/index.html!", ["webapps/v/index.html"]),
    ("3d: out/scene.splat and out/mesh.glb", ["out/scene.splat", "out/mesh.glb"]),
    ("deck out/talk.pptx\nsheet out/t.xlsx", ["out/talk.pptx", "out/t.xlsx"]),
])
def test_detect_widened(text, expected):
    assert [p for p, _ in ai.extract_text_paths(text)] == expected


@pytest.mark.parametrize("text", [
    "plot.png",                                # bare filename, no slash
    "results/*.png",                           # glob
    "out/{city}_x.png",                        # template
    "$OUT/a.png",
    "/path/to/point_cloud.ply",                # placeholder
    "https://example.com/a.png",               # URL
    "out/.thumbcache/a.png.thumb.jpg",
    "out/a.png.thumb.jpg",
    "out/.thumbcache/a.mp4.ios.mp4",
    "/home/u/.xylocopa/uploads/abc_x.png",     # user upload
    "web/node_modules/pkg/logo.png",
    "repo/.git/x.png",
    "results/.../x.png",                       # elided
    "localhost:3000/glass.html",
    "out/a.png.bak",
    ".png/.jpg/.webp",                         # extension list
    "data/x.csv out/y.json notes/z.md",        # non-asset types
])
def test_detect_noise(text):
    assert ai.extract_text_paths(text) == []


def test_backtick_with_spaces_requires_existence():
    got = ai.extract_text_paths("`python plot.py --out results/a.png`")
    assert ("results/a.png", False) in got
    assert ("python plot.py --out results/a.png", True) in got


def test_extract_tool_use_refs():
    meta = {"tool_name": "Write", "tool_use_id": "t1", "file_path": "/r/webapps/index.html"}
    assert ai.extract_message_refs("tool_use", "> `Write` webapps/index.html", json.dumps(meta)) == [
        ("write", "/r/webapps/index.html", False, None)]
    # Legacy row: summary only → relative path must exist to be indexed
    assert ai.extract_message_refs(
        "tool_use", "> `Edit` sub/x.html", json.dumps({"tool_name": "Edit", "tool_use_id": "t"})
    ) == [("write", "sub/x.html", True, None)]
    # Full media path in the summary → absolute, kept even if missing later
    assert ai.extract_message_refs("tool_use", "> `Read` /abs/x.png", None) == [
        ("read", "/abs/x.png", False, None)]
    assert ai.extract_message_refs("tool_use", "> `Bash` python p.py --out a/b.png", None) == []
    send = {"tool_name": "SendUserFile", "tool_use_id": "t", "files": ["/r/a.png", "/r/b.md"]}
    assert ai.extract_message_refs("tool_use", "> `SendUserFile`", json.dumps(send)) == [
        ("send", "/r/a.png", False, None)]
    nb = {"tool_name": "NotebookEdit", "tool_use_id": "t", "file_path": "/r/a.ipynb"}
    assert ai.extract_message_refs("tool_use", "> `NotebookEdit`", json.dumps(nb)) == []
    wa = {"webapp": {"kind": "port", "target": "6006", "project": "demo", "title": "TB"}}
    assert ai.extract_message_refs("webapp", "x", json.dumps(wa))[0][0] == "webapp"
    assert ai.extract_message_refs("stop_hook", "out/a.png", None) == []


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------

def _resolver(env):
    return ai.Resolver({"demo": str(env["root"]), "other": str(env["other"])})


def test_resolve_absolute_in_project(env):
    r = _resolver(env)
    assert r.resolve("demo", None, str(env["root"] / "out" / "plot.png")) == "out/plot.png"
    # Missing absolute path inside the project is still kept (shown as missing)
    assert r.resolve("demo", None, str(env["root"] / "out" / "gone.png")) == "out/gone.png"


def test_resolve_drops_other_project_and_tmp(env):
    r = _resolver(env)
    assert r.resolve("demo", None, str(env["other"] / "x.png")) is None
    assert r.resolve("demo", None, str(env["tmp"] / "scratch" / "tmp.png")) is None
    assert r.resolve("demo", None, "/tmp/claude-1000/x/scratchpad/a.png") is None
    assert r.resolve("demo", None, "other/x.png") is None  # "<other-project>/..." prefix


def test_resolve_relative_with_worktree(env):
    r = _resolver(env)
    assert r.resolve("demo", "wt1", "out/wt_only.png") == ".claude/worktrees/wt1/out/wt_only.png"
    # Worktree candidate wins when both exist
    assert r.resolve("demo", "wt1", "out/plot.png") == ".claude/worktrees/wt1/out/plot.png"
    assert r.resolve("demo", None, "out/plot.png") == "out/plot.png"
    assert r.resolve("demo", None, "./out/plot.png") == "out/plot.png"
    assert r.resolve("demo", None, "demo/out/plot.png") == "out/plot.png"
    assert r.resolve("demo", None, "out/missing.png") == "out/missing.png"
    assert r.resolve("demo", None, "out/missing.png", require_exists=True) is None
    assert r.resolve("demo", None, "/api/files/demo/out/plot.png") == "out/plot.png"


def test_resolve_symlinked_project_root(env, tmp_path):
    real = tmp_path / "real-xylo"
    _touch(str(real / "shots" / "a.png"))
    link = tmp_path / "xylocopa-projects" / "xylo"
    os.symlink(real, link)
    r = ai.Resolver({"xylo": str(link)})
    via_link = r.resolve("xylo", None, str(link / "shots" / "a.png"))
    via_real = r.resolve("xylo", None, str(real / "shots" / "a.png"))
    assert via_link == via_real == "shots/a.png"


def test_resolve_legacy_agenthive_alias(env):
    r = _resolver(env)
    legacy = str(env["tmp"] / "agenthive-projects" / "demo" / "out" / "plot.png")
    assert r.resolve("demo", None, legacy) == "out/plot.png"


def test_resolve_missing_project_dir_skipped(env):
    r = ai.Resolver({"ghost": "/nonexistent/xylocopa-projects/ghost"})
    assert r.resolve("ghost", None, "out/a.png") is None


# ---------------------------------------------------------------------------
# catch_up + list_assets
# ---------------------------------------------------------------------------

def test_catch_up_tiers_and_listing(env, db_session):
    root = env["root"]
    _msg(db_session, f"Saved the chart to {root}/out/plot.png.", minutes=1)
    _msg(db_session, f"> `Read` {root}/out/seen.png", kind="tool_use", minutes=2)
    _msg(db_session, "Report: `docs/report.pdf` and out/gone.png", minutes=3)
    _msg(db_session, "user mentions out/plot.png", role=MessageRole.USER, minutes=4)
    _msg(db_session, "> `Write` out/x.html", kind="tool_use", minutes=5)  # truncated + missing

    changed = ai.catch_up(db_session)
    assert changed == {"demo"}
    refs = _refs(db_session)
    assert {(p, s) for p, s, _ in refs} == {
        ("out/plot.png", "text"), ("out/seen.png", "read"),
        ("docs/report.pdf", "text"), ("out/gone.png", "text"),
    }

    # Idempotent: a second run adds nothing
    assert ai.catch_up(db_session) == set()
    assert _refs(db_session) == refs

    res = _by_path(ai.list_assets(db_session, "demo"))
    assert set(res) == {"out/plot.png", "out/seen.png", "docs/report.pdf"}
    plot = res["out/plot.png"]
    assert plot["tier"] == "delivered" and plot["kind"] == "image" and plot["exists"]
    assert plot["name"] == "plot.png" and plot["dir"] == "out" and plot["ext"] == "png"
    assert plot["size"] == len(PNG_1PX) and plot["mtime"]
    assert plot["last_agent_id"] == "agentmain001"
    assert plot["last_agent_name"] == "Main agent"
    assert plot["first_seen_at"] == "2026-10-01T12:01:00Z"
    assert res["out/seen.png"]["tier"] == "seen"
    assert res["docs/report.pdf"]["kind"] == "pdf"

    full = _by_path(ai.list_assets(db_session, "demo", include_missing=True))
    assert full["out/gone.png"]["exists"] is False
    assert full["out/gone.png"]["size"] is None


def test_last_ref_prefers_top_level_agent(env, db_session):
    root = env["root"]
    m1 = _msg(db_session, f"see {root}/out/plot.png", minutes=1)
    _msg(db_session, f"see {root}/out/plot.png", agent_id="agentsub0001", minutes=5)
    ai.catch_up(db_session)
    a = _by_path(ai.list_assets(db_session, "demo"))["out/plot.png"]
    assert a["last_agent_id"] == "agentmain001"
    assert a["last_message_id"] == m1.id
    assert a["mention_count"] == 2
    assert a["last_seen_at"] == "2026-10-01T12:05:00Z"

    # Asset survives its message being purged; last_message_id drops to the
    # remaining (subagent) mention.
    db_session.delete(m1)
    db_session.commit()
    a = _by_path(ai.list_assets(db_session, "demo"))["out/plot.png"]
    assert a["last_agent_id"] == "agentsub0001"
    assert a["last_message_id"] is not None


def test_last_ref_prefers_delivered_over_later_read(env, db_session):
    root = env["root"]
    m1 = _msg(db_session, f"see {root}/out/plot.png", minutes=1)
    _msg(db_session, f"> `Read` {root}/out/plot.png", kind="tool_use",
         meta={"tool_name": "Read", "tool_use_id": "t1"}, minutes=5)
    ai.catch_up(db_session)
    a = _by_path(ai.list_assets(db_session, "demo"))["out/plot.png"]
    assert a["last_message_id"] == m1.id
    assert a["last_seen_at"] == "2026-10-01T12:05:00Z"


def test_worktree_agent_relative_path(env, db_session):
    _msg(db_session, "made out/wt_only.png", agent_id="agentwt00001")
    ai.catch_up(db_session)
    assert ".claude/worktrees/wt1/out/wt_only.png" in _by_path(ai.list_assets(db_session, "demo"))


def test_overlap_rescans_updated_content(env, db_session):
    m = _msg(db_session, "working on it")
    ai.catch_up(db_session)
    assert _refs(db_session) == []
    m.content = "done: out/plot.png"  # sync_engine streaming update (in place)
    db_session.commit()
    assert ai.catch_up(db_session) == {"demo"}
    assert [p for p, _, _ in _refs(db_session)] == ["out/plot.png"]


def test_version_bump_rebuilds_but_keeps_marks(env, db_session, monkeypatch):
    m = _msg(db_session, "out/plot.png")
    ai.catch_up(db_session)
    ai.set_marks(db_session, "demo", ["out/plot.png"], starred=True)
    # Stale ref on a live message (old rules) → rebuilt away; a ref whose
    # message was purged can't be regenerated → kept.
    db_session.add(AssetRef(project="demo", path="out/bogus.png", source="text",
                            message_id=m.id, created_at=T0))
    db_session.add(AssetRef(project="demo", path="out/seen.png", source="text",
                            message_id="deadbeef0000", created_at=T0))
    db_session.commit()

    monkeypatch.setattr(ai, "ASSET_INDEX_VERSION", ai.ASSET_INDEX_VERSION + 1)
    ai.catch_up(db_session)
    db_session.expire_all()
    assert [p for p, _, _ in _refs(db_session)] == ["out/plot.png", "out/seen.png"]
    assert db_session.get(AssetMark, ("demo", "out/plot.png")).starred is True
    state = json.loads(db_session.get(SystemConfig, ai.STATE_KEY).value)
    assert state["version"] == ai.ASSET_INDEX_VERSION


def test_rowid_renumber_resets_watermark(env, db_session):
    _msg(db_session, "out/plot.png")
    ai.catch_up(db_session)
    db_session.query(AssetRef).delete()
    cfg = db_session.get(SystemConfig, ai.STATE_KEY)
    state = json.loads(cfg.value)
    state["rowid"] = 10_000_000  # beyond max(rowid): rows were renumbered
    cfg.value = json.dumps(state)
    db_session.commit()
    assert ai.catch_up(db_session) == {"demo"}
    assert [p for p, _, _ in _refs(db_session)] == ["out/plot.png"]


def test_missing_starred_asset_stays_visible(env, db_session):
    _msg(db_session, "out/gone.png and out/plot.png")
    ai.catch_up(db_session)
    assert "out/gone.png" not in _by_path(ai.list_assets(db_session, "demo"))
    ai.set_marks(db_session, "demo", ["out/gone.png"], starred=True)
    a = _by_path(ai.list_assets(db_session, "demo"))["out/gone.png"]
    assert a["starred"] is True and a["exists"] is False


def test_marks_batch_set_and_clear(env, db_session):
    paths = ["out/plot.png", "out/seen.png"]
    out = ai.set_marks(db_session, "demo", paths, starred=True)
    assert out == {p: {"starred": True, "hidden": False, "note": None} for p in paths}
    out = ai.set_marks(db_session, "demo", ["out/plot.png"], hidden=True)
    assert out["out/plot.png"] == {"starred": True, "hidden": True, "note": None}
    out = ai.set_marks(db_session, "demo", ["out/seen.png"], note="  keep this  ")
    assert out["out/seen.png"]["note"] == "keep this"
    assert db_session.query(AssetMark).count() == 2

    ai.set_marks(db_session, "demo", paths, starred=False)
    ai.set_marks(db_session, "demo", ["out/plot.png"], hidden=False)
    assert db_session.query(AssetMark).count() == 1  # seen.png still has its note
    out = ai.set_marks(db_session, "demo", ["out/seen.png"], note="")
    assert out["out/seen.png"] == {"starred": False, "hidden": False, "note": None}
    assert db_session.query(AssetMark).count() == 0


def test_webapp_refs(env, db_session):
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    live_port = listener.getsockname()[1]
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    dead_port = probe.getsockname()[1]
    probe.close()
    try:
        db_session.add_all([
            WebApp(id="wastatic0001", project="demo", kind="static",
                   target="webapps/viewer/index.html", title="Viewer",
                   created_by_agent="agentmain001", created_at=T0),
            WebApp(id="waport000001", project="demo", kind="port", target=str(live_port),
                   title="TensorBoard", created_by_agent="agentmain001", created_at=T0),
            WebApp(id="waport000002", project="demo", kind="port", target=str(dead_port),
                   title="", created_by_agent="agentmain001", created_at=T0),
            WebApp(id="waurl0000001", project="demo", kind="url",
                   target="https://wandb.ai/x/run", title="W&B", created_at=T0),
        ])
        db_session.commit()
        _msg(db_session, "🖥️ Web app: Viewer", kind="webapp", meta={"webapp": {
            "kind": "static", "target": "webapps/viewer/index.html",
            "project": "demo", "title": "Viewer"}}, minutes=3)

        assert ai.catch_up(db_session) == {"demo"}
        res = _by_path(ai.list_assets(db_session, "demo", include_missing=True))
        static = res["webapps/viewer/index.html"]
        assert static["kind"] == "webapp" and static["tier"] == "delivered"
        assert static["webapp"] == {"id": "wastatic0001", "kind": "static",
                                    "target": "webapps/viewer/index.html", "title": "Viewer"}
        assert static["exists"] is True
        live = res[f"webapp:port:{live_port}"]
        assert live["exists"] is True and live["name"] == "TensorBoard"
        dead = res[f"webapp:port:{dead_port}"]
        assert dead["exists"] is False and dead["name"] == f"localhost:{dead_port}"
        assert res["webapp:url:https://wandb.ai/x/run"]["exists"] is True
        assert f"webapp:port:{dead_port}" not in _by_path(ai.list_assets(db_session, "demo"))

        # Re-present: registry refs are updated in place, never duplicated
        wa = db_session.get(WebApp, "wastatic0001")
        wa.last_presented_at = T0 + timedelta(days=1)
        db_session.commit()
        ai.catch_up(db_session)
        rows = db_session.query(AssetRef).filter(
            AssetRef.webapp_id == "wastatic0001", AssetRef.message_id.is_(None)).all()
        assert len(rows) == 1 and rows[0].created_at == T0 + timedelta(days=1)

        # Deleted registry row → its message-less ref goes too
        db_session.delete(db_session.get(WebApp, "waurl0000001"))
        db_session.commit()
        ai.catch_up(db_session)
        assert "webapp:url:https://wandb.ai/x/run" not in _by_path(
            ai.list_assets(db_session, "demo", include_missing=True))
    finally:
        listener.close()


def test_webapp_port_src_and_unlinked_keys(env, db_session):
    from routers.preview import port_proxy_sig

    db_session.add_all([
        SystemConfig(key="jwt_secret", value="s3cret"),
        WebApp(id="waport000009", project="demo", kind="port", target="6006",
               title="TB", created_by_agent="agentmain001", created_at=T0),
    ])
    db_session.commit()
    # Cards whose registry row is gone: a port and a url
    _msg(db_session, "🖥️ Web app", kind="webapp", meta={"webapp": {
        "kind": "port", "target": "18998", "project": "demo", "title": "Orphan app"}})
    _msg(db_session, "🖥️ Web app", kind="webapp", meta={"webapp": {
        "kind": "url", "target": "https://example.com/d", "project": "demo",
        "title": "Dash"}}, minutes=1)
    ai.catch_up(db_session)
    res = _by_path(ai.list_assets(db_session, "demo", include_missing=True))

    tb = res["webapp:port:6006"]["webapp"]
    assert tb["src"] == f"/api/preview/p/{port_proxy_sig('s3cret', 'demo', 6006)}/demo/6006/"
    orphan = res["webapp:port:18998"]
    # No registry row → the proxy would 403, so no src; still a port object
    assert orphan["webapp"] == {"id": None, "kind": "port", "target": "18998",
                                "title": "Orphan app"}
    assert orphan["name"] == "Orphan app" and orphan["kind"] == "webapp"
    assert res["webapp:url:https://example.com/d"]["webapp"] == {
        "id": None, "kind": "url", "target": "https://example.com/d", "title": "Dash",
        "src": "https://example.com/d"}


def test_orphan_agent_webapp_does_not_wedge_index(env, db_session):
    db_session.add(Project(name="ghost", display_name="Ghost", path=str(env["tmp"] / "ghost")))
    db_session.commit()
    db_session.add(Agent(id="agentghost01", project="ghost", name="Orphan",
                         mode=AgentMode.AUTO, status=AgentStatus.STOPPED))
    db_session.commit()
    raw = db_session.connection().connection.dbapi_connection
    raw.commit()
    raw.execute("PRAGMA foreign_keys=OFF")
    raw.execute("DELETE FROM projects WHERE name = 'ghost'")  # agents.project has no FK live
    raw.commit()
    raw.execute("PRAGMA foreign_keys=ON")

    _msg(db_session, "🖥️ Web app", agent_id="agentghost01", kind="webapp", meta={
        "webapp": {"kind": "port", "target": "6006", "project": "ghost", "title": "x"}})
    _msg(db_session, "🖥️ Web app", kind="webapp", meta={"webapp": {"kind": "port",
         "target": ["not", "a", "string"], "project": "demo"}}, minutes=1)
    _msg(db_session, "out/plot.png", minutes=2)
    assert ai.catch_up(db_session) == {"demo"}
    assert ai.catch_up(db_session) == set()
    state = json.loads(db_session.get(SystemConfig, ai.STATE_KEY).value)
    assert state["rowid"] == raw.execute("SELECT MAX(rowid) FROM messages").fetchone()[0]
    assert [p for p, _, _ in _refs(db_session)] == ["out/plot.png"]


def test_insert_refs_skips_vanished_project(env, db_session):
    raw = db_session.connection().connection.dbapi_connection
    when = T0.isoformat(sep=" ")
    changed = ai._insert_refs(raw, [
        ("gone", "out/a.png", "text", "m00000000001", None, None, when),
        ("demo", "out/plot.png", "text", "m00000000002", None, None, when),
    ])
    assert changed == {"demo"}
    assert _refs(db_session) == [("out/plot.png", "text", "m00000000002")]


def test_resolve_one_level_subdir_fallback(env):
    _touch(str(env["root"] / "recovered" / "run_video" / "a.mp4"), b"x")
    r = ai.Resolver({"demo": str(env["root"])})
    # Same probe as files._resolve_project_file "Fallback 2" (chat previews it)
    assert r.resolve("demo", None, "run_video/a.mp4") == "recovered/run_video/a.mp4"
    assert r.resolve("demo", None, "`run_video/a.mp4`", require_exists=True) == \
        "recovered/run_video/a.mp4"
    assert r.resolve("demo", None, "out/plot.png") == "out/plot.png"  # direct hit wins
    assert r.resolve("demo", None, "nowhere/x.png") == "nowhere/x.png"  # still missing


def test_refless_marks_are_listed(env, db_session):
    ai.set_marks(db_session, "demo", ["out/seen.png", "out/gone.png", "webapp:port:7007",
                                      "../other/x.png", "/etc/x.png", "notes.txt",
                                      "webapp:url:javascript:alert(1)"], starred=True)
    res = _by_path(ai.list_assets(db_session, "demo"))
    assert set(res) == {"out/seen.png", "out/gone.png", "webapp:port:7007"}
    seen = res["out/seen.png"]
    assert seen["sources"] == [] and seen["tier"] == "delivered"
    assert seen["exists"] is True and seen["starred"] is True and seen["kind"] == "image"
    assert seen["last_seen_at"] and seen["last_agent_id"] is None
    assert res["out/gone.png"]["exists"] is False
    port = res["webapp:port:7007"]
    assert port["kind"] == "webapp" and port["webapp"]["kind"] == "port"


def test_set_marks_concurrent_same_path(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from sqlalchemy import create_engine, event
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import NullPool
    from models import Base

    engine = create_engine(f"sqlite:///{tmp_path / 'marks.db'}",
                           connect_args={"check_same_thread": False}, poolclass=NullPool)

    @event.listens_for(engine, "connect")
    def _pragma(dbapi_conn, _rec):
        dbapi_conn.execute("PRAGMA foreign_keys=ON")
        dbapi_conn.execute("PRAGMA busy_timeout=5000")

    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    with Session() as s:
        s.add(Project(name="demo", display_name="Demo", path=str(tmp_path)))
        s.commit()

    def mark(path, **fields):
        with Session() as s:
            return ai.set_marks(s, "demo", [path], **fields)

    with ThreadPoolExecutor(2) as ex:
        futs = []
        for i in range(30):
            futs.append(ex.submit(mark, f"out/{i}.png", starred=True))
            futs.append(ex.submit(mark, f"out/{i}.png", hidden=True))
        for f in futs:
            f.result()  # no UNIQUE IntegrityError
    with Session() as s:
        rows = s.query(AssetMark).all()
        assert len(rows) == 30 and all(r.starred and r.hidden for r in rows)
    engine.dispose()


def test_turns_prefilter():
    assert ai.turns_may_have_assets([("assistant", "see out/a.png", None, None, "text", None)])
    assert ai.turns_may_have_assets([("assistant", "> `Write`", {"file_path": "/x/a.md"},
                                      None, "tool_use", None)])
    assert not ai.turns_may_have_assets([("assistant", "all done", None, None, "text", None)])
    assert not ai.turns_may_have_assets([("user", "out/a.png", None, None, None, None)])


# ---------------------------------------------------------------------------
# jsonl_parser forward fix
# ---------------------------------------------------------------------------

def test_tool_meta_carries_full_paths():
    from jsonl_parser import parse_session_turns_from_lines

    def tool(name, inp, tid):
        return json.dumps({"type": "assistant", "uuid": f"u-{tid}", "sessionId": "s",
                           "message": {"content": [{"type": "tool_use", "id": tid,
                                                    "name": name, "input": inp}]}})

    lines = [
        tool("Write", {"file_path": "/p/docs/deep/report.html", "content": "x"}, "t1"),
        tool("NotebookEdit", {"notebook_path": "/p/nb/a.ipynb", "new_source": "x"}, "t2"),
        tool("SendUserFile", {"files": ["/p/a.png", "/p/b.pdf"], "caption": "c"}, "t3"),
        tool("Bash", {"command": "ls"}, "t4"),
    ]
    turns = [t for t in parse_session_turns_from_lines(lines) if t[4] == "tool_use"]
    by_id = {t[2]["tool_use_id"]: t for t in turns}
    # Chat summary unchanged (still truncated); full path rides in metadata
    assert by_id["t1"][1] == "> `Write` deep/report.html"
    assert by_id["t1"][2]["file_path"] == "/p/docs/deep/report.html"
    assert by_id["t2"][2]["file_path"] == "/p/nb/a.ipynb"
    assert by_id["t3"][2]["files"] == ["/p/a.png", "/p/b.pdf"]
    assert by_id["t4"][2] == {"tool_name": "Bash", "tool_use_id": "t4"}


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_api_list_and_marks(client, env, db_session):
    _msg(db_session, "out/plot.png and out/gone.png")

    resp = await client.get("/api/projects/demo/assets")
    assert resp.status_code == 200
    body = resp.json()
    assert body["indexed_at"].endswith("Z")
    assert [a["path"] for a in body["assets"]] == ["out/plot.png"]

    resp = await client.get("/api/projects/demo/assets?include_missing=1")
    assert {a["path"] for a in resp.json()["assets"]} == {"out/plot.png", "out/gone.png"}

    resp = await client.post("/api/projects/demo/assets/marks", json={
        "paths": ["out/plot.png", "out/gone.png"], "starred": True, "hidden": None})
    assert resp.status_code == 200
    assert resp.json()["marks"]["out/gone.png"] == {"starred": True, "hidden": False, "note": None}

    resp = await client.get("/api/projects/demo/assets")
    assets = _by_path(resp.json())
    assert assets["out/gone.png"]["starred"] and not assets["out/gone.png"]["exists"]

    resp = await client.post("/api/projects/demo/assets/marks",
                             json={"paths": ["out/plot.png"], "note": "hero shot"})
    assert resp.json()["marks"]["out/plot.png"]["note"] == "hero shot"


@pytest.mark.anyio
async def test_api_get_survives_catch_up_failure_and_emits(client, env, db_session,
                                                           monkeypatch):
    import asyncio
    import websocket

    emitted = []

    async def fake_emit(project):
        emitted.append(project)

    monkeypatch.setattr(websocket, "emit_assets_changed", fake_emit)
    _msg(db_session, "out/plot.png")
    resp = await client.get("/api/projects/demo/assets")
    assert resp.status_code == 200
    await asyncio.sleep(0.05)
    assert emitted == ["demo"]  # refs indexed by a GET are announced too

    def boom(db=None):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(ai, "catch_up", boom)
    resp = await client.get("/api/projects/demo/assets")
    assert resp.status_code == 200
    assert [a["path"] for a in resp.json()["assets"]] == ["out/plot.png"]


@pytest.mark.anyio
async def test_api_validation(client, env):
    assert (await client.get("/api/projects/nope/assets")).status_code == 404
    r = await client.post("/api/projects/nope/assets/marks",
                          json={"paths": ["a.png"], "starred": True})
    assert r.status_code == 404
    r = await client.post("/api/projects/demo/assets/marks", json={"paths": [], "starred": True})
    assert r.status_code == 422
    r = await client.post("/api/projects/demo/assets/marks",
                          json={"paths": ["a.png"] * 1001, "starred": True})
    assert r.status_code == 422
    r = await client.post("/api/projects/demo/assets/marks", json={"paths": ["a.png"]})
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# Project lifecycle registration
# ---------------------------------------------------------------------------

def _seed_asset_rows(db, name):
    db.add(AssetRef(project=name, path="out/a.png", source="text",
                    message_id="m00000000001", created_at=T0))
    db.add(AssetMark(project=name, path="out/a.png", starred=True))
    db.commit()


@pytest.mark.anyio
async def test_rename_project_moves_asset_rows(client, db_session, tmp_path, monkeypatch):
    import dropbox_sync.engine as dbx
    import routers.projects as rp

    async def _no_remote_move(old, new):  # would build a real Dropbox HTTPS client
        return None

    monkeypatch.setattr(rp, "PROJECT_CONFIGS_PATH", str(tmp_path))
    monkeypatch.setattr(dbx, "on_project_renamed", _no_remote_move)
    root = tmp_path / "root-dir"  # path doesn't end in the name → no dir move
    root.mkdir()
    db_session.add(Project(name="asset-old", display_name="asset-old", path=str(root)))
    db_session.commit()
    _seed_asset_rows(db_session, "asset-old")

    r = await client.put("/api/projects/asset-old/rename", json={"new_name": "asset-new"})
    assert r.status_code == 200, r.text
    db_session.expire_all()
    assert {x.project for x in db_session.query(AssetRef).all()} == {"asset-new"}
    assert {x.project for x in db_session.query(AssetMark).all()} == {"asset-new"}


@pytest.mark.anyio
async def test_delete_project_removes_asset_rows(client, db_session, tmp_path, monkeypatch):
    import config
    import dropbox_sync.engine as dbx
    import routers.projects as rp

    async def _no_forget(name):  # keep the Dropbox engine singletons untouched
        return None

    monkeypatch.setattr(rp, "PROJECT_CONFIGS_PATH", str(tmp_path))
    monkeypatch.setattr(config, "PROJECTS_DIR", str(tmp_path))
    monkeypatch.setattr(dbx, "on_project_deleted", _no_forget)
    root = tmp_path / "zz-asset-del"
    root.mkdir()
    db_session.add(Project(name="zz-asset-del", display_name="x", path=str(root)))
    db_session.commit()
    _seed_asset_rows(db_session, "zz-asset-del")

    r = await client.delete("/api/projects/zz-asset-del")
    assert r.status_code == 200, r.text
    db_session.expire_all()
    assert db_session.query(AssetRef).count() == 0
    assert db_session.query(AssetMark).count() == 0


def test_reconcile_scan_without_asset_tables_keeps_flushed_work(db_session):
    from sqlalchemy import text
    import reconcile

    db_session.add(Project(name="gone", display_name="Gone", path="/nonexistent/gone"))
    db_session.commit()
    db_session.execute(text("DROP TABLE asset_refs"))
    db_session.execute(text("DROP TABLE asset_marks"))
    db_session.commit()
    # apply_all flushes project-row deletes before rescanning FK orphans
    db_session.query(Project).filter(Project.name == "gone").delete()
    db_session.flush()
    out = reconcile._scan_fk_orphans(db_session)
    assert out["asset_refs_bad_project"] == [] and out["asset_marks_bad_project"] == []
    assert db_session.query(Project).filter(Project.name == "gone").count() == 0


# ---------------------------------------------------------------------------
# Thumbnails (?tile=1, EXIF orientation)
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_thumbnail_sizes_and_exif(client, env):
    from io import BytesIO
    from PIL import Image

    root = env["root"]
    big = Image.new("RGB", (1000, 500), "red")
    big.paste((0, 255, 0), (0, 0, 250, 500))  # green left quarter: cropped away
    big.save(root / "out" / "big.png")
    exif = Image.Exif()
    exif[0x0112] = 6  # rotate 90° CW on display
    Image.new("RGB", (200, 100), "blue").save(root / "out" / "rot.jpg", exif=exif)

    r = await client.get("/api/thumbs/demo/out/big.png?tile=1")
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg"
    tile = Image.open(BytesIO(r.content))
    assert tile.size == (400, 400)
    r_, g, _b = tile.convert("RGB").getpixel((5, 200))
    assert r_ > 200 and g < 60  # center crop: the green edge is gone
    assert (root / "out" / ".thumbcache" / "big.png.tile400.thumb.jpg").is_file()

    r = await client.get("/api/thumbs/demo/out/big.png")
    assert Image.open(BytesIO(r.content)).size == (1000, 500)
    assert (root / "out" / ".thumbcache" / "big.png.thumb.jpg").is_file()

    r = await client.get("/api/thumbs/demo/out/rot.jpg?tile=1")  # small: never upscaled
    assert Image.open(BytesIO(r.content)).size == (100, 100)
    r = await client.get("/api/thumbs/demo/out/rot.jpg")
    assert Image.open(BytesIO(r.content)).size == (100, 200)
    leftovers = [f for f in os.listdir(root / "out" / ".thumbcache") if f.endswith(".tmp")]
    assert leftovers == []


def test_warm_tiles_renders_delivered_media_only(env, db_session):
    from PIL import Image
    root = env["root"]
    Image.new("RGB", (800, 400), "red").save(root / "out" / "plot.png")
    _msg(db_session, f"Saved the chart to {root}/out/plot.png.", minutes=1)
    _msg(db_session, f"> `Read` {root}/out/seen.png", kind="tool_use", minutes=2)
    _msg(db_session, "Report: `docs/report.pdf`", minutes=3)
    _msg(db_session, "other project: other/x.png", minutes=4)
    ai.catch_up(db_session)
    cache = root / "out" / ".thumbcache"

    assert ai.warm_tiles({"demo"}, db=db_session) == 1
    assert (cache / "plot.png.tile400.thumb.jpg").is_file()
    assert not (cache / "seen.png.tile400.thumb.jpg").exists()   # seen tier: on demand
    assert not (root / "docs" / ".thumbcache").exists()          # pdf: no tile thumb
    assert ai.warm_tiles({"demo"}, db=db_session) == 0           # fresh: skipped
    assert ai.warm_tiles({"other"}, db=db_session) == 0

    os.utime(root / "out" / "plot.png", (2e9, 2e9))              # source edited
    assert ai.warm_tiles(None, db=db_session) == 1


def test_schedule_tile_warm_coalesces(monkeypatch):
    import threading
    import time
    calls = []
    gate = threading.Event()

    def fake_warm(projects=None, db=None):
        calls.append(projects)
        gate.wait(2)
        return 0

    monkeypatch.setattr(ai, "warm_tiles", fake_warm)
    _real_schedule_tile_warm({"a"})
    deadline = time.time() + 2
    while not calls and time.time() < deadline:
        time.sleep(0.01)
    _real_schedule_tile_warm({"b"})
    _real_schedule_tile_warm({"c"})
    gate.set()
    deadline = time.time() + 3
    while ai._warm_running and time.time() < deadline:
        time.sleep(0.01)
    assert calls == [{"a"}, {"b", "c"}]

    calls.clear()
    _real_schedule_tile_warm()  # all projects
    deadline = time.time() + 3
    while (ai._warm_running or not calls) and time.time() < deadline:
        time.sleep(0.01)
    assert calls == [None]


# ---------------------------------------------------------------------------
# schedule_catch_up (coalescing + WS emit)
# ---------------------------------------------------------------------------

def _wait_idle(timeout=3.0):
    import time
    deadline = time.time() + timeout
    while ai._sched_running and time.time() < deadline:
        time.sleep(0.01)
    assert not ai._sched_running


def test_schedule_catch_up_coalesces_bursts(monkeypatch):
    import threading
    calls = []
    gate = threading.Event()

    def fake_catch_up(db=None):
        calls.append(1)
        gate.wait(2)
        return set()

    monkeypatch.setattr(ai, "catch_up", fake_catch_up)
    ai.schedule_catch_up()
    for _ in range(5):
        ai.schedule_catch_up()  # in flight → marks dirty only
    gate.set()
    _wait_idle()
    assert len(calls) == 2


def test_schedule_catch_up_never_raises(monkeypatch):
    def boom(db=None):
        raise RuntimeError("db gone")

    monkeypatch.setattr(ai, "catch_up", boom)
    ai.schedule_catch_up()
    _wait_idle()


@pytest.mark.anyio
async def test_schedule_catch_up_emits_assets_changed(monkeypatch):
    import asyncio
    import websocket

    emitted = []

    async def fake_emit(project):
        emitted.append(project)

    monkeypatch.setattr(ai, "catch_up", lambda db=None: {"demo", "other"})
    monkeypatch.setattr(websocket, "emit_assets_changed", fake_emit)
    ai.schedule_catch_up()
    for _ in range(100):
        if len(emitted) == 2:
            break
        await asyncio.sleep(0.01)
    assert sorted(emitted) == ["demo", "other"]
