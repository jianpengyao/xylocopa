"""Tests for claude.ai artifacts in the project Assets index
(artifact_index.py + asset_index.py + GET /assets).

Synthetic URLs, ids and tmp_path transcripts only. An autouse fixture points
transcript lookup at tmp_path, so no test can reach real transcripts.
"""

import json
import os
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

import artifact_index as xi
import asset_index as ai
from models import (
    Agent, AgentMode, AgentStatus, ArtifactCall, AssetRef, CCSession, Message,
    MessageRole, MessageStatus, Project, SystemConfig,
)

A1 = "https://claude.ai/artifact/TestArtifactAAAAAAAAAA"
A2 = "https://claude.ai/artifact/TestArtifactBBBBBBBBBB"
A3 = "https://claude.ai/artifact/TestArtifactCCCCCCCCCC"
UUID1 = "00000000-0000-4000-8000-000000000001"
CODE1 = f"https://claude.ai/code/artifact/{UUID1}"
TYPE_SLIDES = "https://claude.ai/artifact/TestTypeSlidesAAAAAAAA"
TYPE_DS = "https://claude.ai/artifact/TestTypeDesignSysAAAAA"
K1 = "artifact:" + A1

# Recent enough that a pending call is still inside ARTIFACT_PENDING_TTL_S.
BASE = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=10)
PNG_1PX = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06"
    b"\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\rIDATx\x9cc\xf8\x0f\x00\x00\x01\x01"
    b"\x00\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82"
)

_real_arm_artifact_retry = ai._arm_artifact_retry


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def iso(monkeypatch, tmp_path):
    """Fail-closed isolation: transcripts resolve into tmp_path only, no
    backup copies, no retry timer, no tile warm-up, and OVERLAP 0 so a
    second catch_up is truly incremental."""
    armed: list[int] = []
    monkeypatch.setattr(ai, "_arm_artifact_retry", lambda: armed.append(1))
    monkeypatch.setattr(ai, "schedule_tile_warm", lambda projects=None: None)
    monkeypatch.setattr(ai, "OVERLAP", 0)
    monkeypatch.setattr(ai, "_artifact_timer", None)
    monkeypatch.setattr(ai, "_artifact_retry_idx", 0)
    tdir = tmp_path / "claude" / "projects" / "demo"
    tdir.mkdir(parents=True)

    def resolve(session_id, project_path, worktree=None, cwd=None):
        return str(tdir / f"{session_id}.jsonl")

    monkeypatch.setattr(xi, "_resolve_jsonl", resolve)
    monkeypatch.setattr(xi, "_backup_twin", lambda path: None)
    return SimpleNamespace(armed=armed, tdir=tdir)


@pytest.fixture()
def env(tmp_path, db_session):
    base = tmp_path / "xylocopa-projects"
    root = base / "demo"
    (root / "out").mkdir(parents=True)
    (root / "out" / "plot.png").write_bytes(PNG_1PX)
    other = base / "other"
    other.mkdir(parents=True)
    db_session.add_all([
        Project(name="demo", display_name="Demo", path=str(root)),
        Project(name="other", display_name="Other", path=str(other)),
    ])
    db_session.commit()

    def agent(aid, name, project="demo", sid=None, **kw):
        return Agent(id=aid, project=project, name=name, mode=AgentMode.AUTO,
                     status=AgentStatus.STOPPED, session_id=sid, **kw)

    db_session.add_all([
        agent("agentmain001", "Main agent", sid="s-main"),
        agent("agentfork001", "Fork agent", sid="s-fork"),
        agent("agentsec0001", "Second agent", sid="s-sec"),
        agent("agentoth0001", "Other agent", project="other", sid="s-oth"),
    ])
    db_session.commit()
    db_session.add(agent("agentsub0001", "Subagent", is_subagent=True,
                         parent_id="agentmain001", claude_agent_id="atest0001"))
    db_session.commit()
    return {"root": root, "other": other}


def _ts(seconds=0):
    return BASE + timedelta(seconds=seconds)


def _msg(db, content, agent="agentmain001", kind="text", meta=None, tid=None, seconds=0):
    m = Message(
        agent_id=agent, role=MessageRole.AGENT, content=content, kind=kind,
        meta_json=json.dumps(meta) if meta is not None else None, tool_use_id=tid,
        status=MessageStatus.COMPLETED, created_at=_ts(seconds),
    )
    db.add(m)
    db.commit()
    return m


def _tool_row(db, tid, agent="agentmain001", seconds=0):
    return _msg(db, "> `Artifact`", agent=agent, kind="tool_use",
                meta={"tool_name": "Artifact", "tool_use_id": tid}, tid=tid, seconds=seconds)


def _use(tid, inp):
    return {"type": "assistant", "sessionId": "s-test", "timestamp": "2026-01-01T00:00:00.000Z",
            "message": {"role": "assistant", "content": [
                {"type": "tool_use", "id": tid, "name": "Artifact", "input": inp}]}}


_NO_TUR = object()


def _result(tid, tur=_NO_TUR, text="", is_error=False):
    e = {"type": "user", "sessionId": "s-test", "timestamp": "2026-01-01T00:00:01.000Z",
         "message": {"role": "user", "content": [
             {"type": "tool_result", "tool_use_id": tid, "is_error": is_error,
              "content": [{"type": "text", "text": text}]}]}}
    if tur is not _NO_TUR:
        e["toolUseResult"] = tur
    return e


def _append(path, *entries):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a") as f:
        for e in entries:
            f.write((e if isinstance(e, str) else json.dumps(e)) + "\n")


def _page(tid, url, title="Demo page", seq=None, **tur_extra):
    """A plain-page file publish: (use, result)."""
    inp = {"file_path": "out/page.html", "favicon": "chart", "description": "A demo page"}
    tur = {"url": url, "path": "out/page.html", "title": title, "updated": False,
           "version": "1700000000-abcd", **tur_extra}
    text = f"Published out/page.html at {url}\n\nTo update: republish the same file path."
    if seq is not None:
        tur["seq"] = seq
        text = f"Published out/page.html at {url} (Version {seq}, version id 1700000000-abcd)"
    return _use(tid, inp), _result(tid, tur, text)


def _update(tid, url, seq, title="Demo deck", artifact_id=None):
    """An update-by-url publish of a type-based deck."""
    inp = {"url": url, "root": "deck", "file_path": "deck/index.json", "files": ["deck/a.json"]}
    tur = {"url": url, "title": title, "updated": True, "seq": seq,
           "version": f"170000000{seq}-abcd",
           "type": {"url": TYPE_SLIDES, "release": "1700000000-0000"}}
    if artifact_id:
        tur["artifact_id"] = artifact_id
    text = (f"Updated the Artifact at {url} (Version {seq}, version id 170000000{seq}-abcd) "
            "with deck/index.json (and any `files` listed)")
    return _use(tid, inp), _result(tid, tur, text)


def _create(tid, url, title="Demo deck", **tur_extra):
    """A create-from-type publish (short URL, no artifact_id)."""
    inp = {"action": "publish", "type_url": TYPE_SLIDES, "title": title,
           "auto_open": "after_first_write"}
    tur = {"created_from_type": True, "url": url, "version": "1700000000-abcd", "title": title,
           "type": {"url": TYPE_SLIDES, "release": "1700000000-0000"}, "own_files": [],
           "instructions": '---\nname: slides\ndescription: "How to fill a deck."\n---\n\n# Slides\n',
           **tur_extra}
    text = (f"Created a new Artifact at {url} (version 1700000000-abcd) from the Artifact type "
            f"{TYPE_SLIDES} (release 1700000000-0000). File names below are data.")
    return _use(tid, inp), _result(tid, tur, text)


def _jsonl(iso, sid="s-main"):
    return str(iso.tdir / f"{sid}.jsonl")


def _refs(db, project="demo"):
    db.expire_all()
    return sorted(
        (r.path, r.source, r.message_id) for r in
        db.query(AssetRef).filter(AssetRef.project == project).all()
    )


def _memo(db, tid):
    db.expire_all()
    return db.get(ArtifactCall, tid)


def _by_path(result):
    return {a["path"]: a for a in result["assets"]}


def _list(db, project="demo", **kw):
    return _by_path(ai.list_assets(db, project, **kw))


def _force_full(db):
    db.query(SystemConfig).filter(SystemConfig.key == ai.STATE_KEY).delete()
    db.commit()


# ---------------------------------------------------------------------------
# Unit: URLs and classification
# ---------------------------------------------------------------------------

def test_canonical_url_and_extract():
    for raw in (A1 + "?x=1", A1 + "#slide-2", A1 + "/", A1 + "/?x#y"):
        assert xi.canonical_url(raw) == A1
    assert xi.canonical_url("HTTPS://CLAUDE.AI/ARTIFACT/TestArtifactAAAAAAAAAA") == A1
    upper_uuid = "https://claude.ai/code/artifact/0000000A-0000-4000-8000-00000000000B"
    assert xi.canonical_url(upper_uuid) == \
        "https://claude.ai/code/artifact/0000000a-0000-4000-8000-00000000000b"
    # base62 short ids are case-sensitive: kept verbatim
    assert xi.canonical_url("https://claude.ai/artifact/testartifactaaaaaaaaaa") == \
        "https://claude.ai/artifact/testartifactaaaaaaaaaa"
    for bad in ("http://claude.ai/artifact/TestArtifactAAAAAAAAAA",
                "https://example.com/artifact/TestArtifactAAAAAAAAAA",
                "https://claude.ai/artifacts/TestArtifactAAAAAAAAAA",
                "https://claude.ai/artifact/short", None, 42):
        assert xi.canonical_url(bad) is None
    assert xi.uuid_of(CODE1) == UUID1 and xi.uuid_of(A1) is None

    text = (f"see [deck]({A1}) and **{A1}**。 also {CODE1}：done ({A2}) "
            f"{A3}?x=1 {A1}#frag")
    assert xi.extract_urls(text) == [A1, CODE1, A2, A3]
    assert xi.extract_urls("no links here") == []
    assert xi.is_canonical_key(K1)
    assert not xi.is_canonical_key("artifact:" + A1 + "?x=1")
    assert not xi.is_canonical_key("artifact:javascript:x")


def test_classify_call_variants():
    def cls(entries, known=()):
        use, res = entries
        r = {k: v for k, v in res["message"]["content"][0].items()}
        out = {"is_error": r["is_error"], "text": xi._result_text(r["content"])}
        if "toolUseResult" in res:
            out["toolUseResult"] = res["toolUseResult"]
        return xi.classify_call(use["message"]["content"][0]["input"], out, known)

    c = cls(_page("t1", A1, title="Page"))
    assert (c["outcome"], c["url"], c["title"], c["type_url"]) == ("publish", A1, "Page", None)
    assert c["description"] == "A demo page" and c["version"] == "1700000000-abcd"

    c = cls(_update("t2", CODE1, 4, artifact_id=UUID1))
    assert (c["outcome"], c["seq"], c["artifact_id"], c["type_url"]) == \
        ("publish", 4, UUID1, TYPE_SLIDES)
    assert c["types"] == [[TYPE_SLIDES, None]]

    c = cls(_create("t3", A2))
    assert (c["outcome"], c["url"], c["type_url"], c["type_name"]) == \
        ("publish", A2, TYPE_SLIDES, "slides")
    assert xi.kind_of(c["type_url"], c["type_name"]) == "slides"

    def call(inp, tur=_NO_TUR, text="", is_error=False, known=()):
        res = {"is_error": is_error, "text": text}
        if tur is not _NO_TUR:
            res["toolUseResult"] = tur
        return xi.classify_call(inp, res, known)

    # Asset variants never become artifacts
    assert call({"url": A1, "asset": True, "file_path": "a.png"},
                {"written": {"url": A1}, "asset_upload": {"id": "x"}},
                f"Asset uploaded: a.png ... {A1}")["outcome"] == "asset"
    assert call({"url": A1, "from_url": A2, "asset_ids": ["0" * 32]},
                {"written": {"url": A1}, "asset_copy": {"from_url": A2}},
                f"Copied 1 assets from {A2} into {A1}")["outcome"] == "asset"
    assert call({"action": "delete", "url": A1, "path": "0" * 32},
                {"written": {"url": A1}, "asset_delete": {"id": "x", "deleted": True}},
                "Asset deleted: _blob/x is gone")["outcome"] == "asset"

    # Reads, lists and quickstarts are ignored but record their type pairs
    c = call({"action": "read", "url": A1, "paths": ["index.html"]},
             {"files_read": {"url": A1, "title": "Deck", "type": {"url": TYPE_SLIDES,
                                                                  "title": "Slides"}}},
             f"Read {A1}")
    assert c["outcome"] == "ignored" and c["types"] == [[TYPE_SLIDES, "Slides"]]
    c = call({"action": "list", "url": A1, "scope": "files"},
             {"file_list": {"url": A1, "type": {"url": TYPE_SLIDES, "title": "Slides"}}})
    assert c["outcome"] == "ignored" and c["types"] == [[TYPE_SLIDES, "Slides"]]
    c = call({"action": "quickstart", "intent": "slides"},
             {"quickstart": {"match": {"title": "Slides", "type_url": TYPE_SLIDES},
                             "design_systems": {"type": "Design System", "type_url": TYPE_DS}}},
             f"Quickstart. type_url: {TYPE_SLIDES}")
    assert c["outcome"] == "ignored"
    assert sorted(c["types"]) == sorted([[TYPE_SLIDES, "Slides"], [TYPE_DS, "Design System"]])

    # Errors
    assert call({"file_path": "a.html"}, "favicon required",
                "<tool_use_error>favicon required to publish</tool_use_error>")["outcome"] == "error"
    assert call({"file_path": "a.html"}, text="<tool_use_error>x</tool_use_error>",
                )["outcome"] == "error"
    assert call({"file_path": "a.html"}, {"url": A1}, f"Published a.html at {A1}",
                is_error=True)["outcome"] == "error"

    # Text-only result (no toolUseResult key) still publishes
    c = call({"file_path": "a.html", "title": "From input"}, text=f"Published a.html at {A1}")
    assert (c["outcome"], c["url"], c["title"]) == ("publish", A1, "From input")

    # A dict with url but no success sentence naming it: unconfirmed
    c = call({"url": A1, "file_path": "a.html"}, {"url": A1, "title": "Live"},
             "Refused: a newer version exists; here is the live version.")
    assert (c["outcome"], c["reason"]) == ("ignored", "unconfirmed")
    c = call({"file_path": "a.html"}, {"url": A1}, f"Published a.html at {A2}")
    assert (c["outcome"], c["reason"]) == ("ignored", "unconfirmed")

    # A banner before the success sentence is tolerated
    c = call({"file_path": "a.html"}, {"url": A1, "title": "T"},
             f"[Version 1700000001-ffff of this artifact was published after the version you "
             f"have.]\n\nPublished a.html at {A1}")
    assert c["outcome"] == "publish"

    # Whole-artifact delete (never observed; best effort)
    c = call({"action": "delete", "url": A1 + "?x=1"}, {"deleted": True}, "Deleted.")
    assert (c["outcome"], c["url"]) == ("deleted", A1)

    # A URL equal to a known type is never an artifact
    c = call({"file_path": "a.html"}, {"url": TYPE_SLIDES}, f"Published a.html at {TYPE_SLIDES}",
             known={TYPE_SLIDES: "Slides"})
    assert (c["outcome"], c["reason"]) == ("ignored", "type_url")

    # Odd shapes don't raise: a string `type`, a non-int seq
    c = call({"file_path": "a.html"}, {"url": A1, "type": "Slides", "seq": "3"},
             f"Published a.html at {A1} (Version 3, version id v)")
    assert (c["outcome"], c["type_url"], c["seq"]) == ("publish", None, 3)
    c = call({"file_path": "a.html"}, {"url": {"nested": A1}}, f"Published a.html at {A1}")
    assert (c["outcome"], c["reason"]) == ("ignored", "unconfirmed")
    assert call(["not", "a", "dict"], {"url": A1}, f"Published a at {A1}")["outcome"] == "publish"


# ---------------------------------------------------------------------------
# Integration: catch_up + list_assets
# ---------------------------------------------------------------------------

def test_publish_indexed_from_jsonl(env, db_session, iso):
    _append(_jsonl(iso), *_page("toolu_test_001", A1, title="Demo page"))
    m = _tool_row(db_session, "toolu_test_001")

    assert ai.catch_up(db_session) == {"demo"}
    assert _refs(db_session) == [(K1, "artifact", m.id)]
    assert _memo(db_session, "toolu_test_001").outcome == "publish"

    row = _list(db_session)[K1]
    assert row["kind"] == "artifact" and row["tier"] == "delivered"
    assert row["name"] == "Demo page" and row["dir"] == "" and row["ext"] == ""
    assert row["size"] is None and row["mtime"] is None and row["exists"] is True
    assert row["sources"] == ["artifact"] and row["webapp"] is None
    assert row["last_message_id"] == m.id and row["last_agent_id"] == "agentmain001"
    art = row["artifact"]
    assert art["url"] == A1 and art["kind"] == "page" and art["title"] == "Demo page"
    assert art["description"] == "A demo page" and art["publish_count"] == 1
    assert art["published_by_agent_id"] == "agentmain001"
    assert art["published_by_agent_name"] == "Main agent"
    assert art["first_published_at"].endswith("Z") and art["deleted"] is False
    assert ai._artifact_pending == 0 and iso.armed == []
    # Non-artifact rows carry "artifact": null
    _msg(db_session, "out/plot.png", seconds=5)
    ai.catch_up(db_session)
    assert _list(db_session)["out/plot.png"]["artifact"] is None


def test_type_urls_never_indexed(env, db_session, iso):
    q_use = _use("toolu_test_q", {"action": "quickstart", "intent": "slides"})
    q_res = _result("toolu_test_q", {"quickstart": {
        "intent": "slides",
        "match": {"title": "Slides", "type_url": TYPE_SLIDES, "tier": "match"},
        "design_systems": {"type": "Design System", "type_url": TYPE_DS, "scope": "account",
                           "instances": []},
    }}, f"Quickstart for a slide deck.\ntype_url: {TYPE_SLIDES}")
    _append(_jsonl(iso), q_use, q_res, *_create(
        "toolu_test_c", A1, design_systems={"type": "Design System", "type_url": TYPE_DS,
                                            "scope": "account", "instances": []},
        design_system={"unavailable": "none"}))
    _tool_row(db_session, "toolu_test_q", seconds=1)
    _tool_row(db_session, "toolu_test_c", seconds=2)
    _msg(db_session, f"Type {TYPE_SLIDES}, design system {TYPE_DS}, deck {A1}", seconds=3)

    ai.catch_up(db_session)
    assert {p for p, _, _ in _refs(db_session)} == {K1}
    row = _list(db_session)[K1]
    assert row["artifact"]["kind"] == "slides" and row["artifact"]["type_name"] == "Slides"
    assert row["sources"] == ["artifact", "text"]

    type_urls = set()
    for c in _memo(db_session, "toolu_test_q"), _memo(db_session, "toolu_test_c"):
        type_urls.update(u for u, _ in json.loads(c.types_json or "[]"))
        if c.type_url:
            type_urls.add(c.type_url)
    assert type_urls == {TYPE_SLIDES, TYPE_DS}
    all_paths = {r.path for r in db_session.query(AssetRef).all()}
    assert not any(u in p for u in type_urls for p in all_paths)


def test_pending_then_resolved_incremental(env, db_session, iso, monkeypatch):
    use, res = _page("toolu_test_001", A1)
    _append(_jsonl(iso), use)
    m = _tool_row(db_session, "toolu_test_001")

    assert ai.catch_up(db_session) == set()
    assert _memo(db_session, "toolu_test_001").outcome == "pending"
    assert _refs(db_session) == []
    assert iso.armed == [1] and ai._artifact_pending == 1

    extracted = []
    real_extract = ai.extract_message_refs

    def spy_extract(kind, content, meta_raw):
        extracted.append((kind, content))
        return real_extract(kind, content, meta_raw)

    monkeypatch.setattr(ai, "extract_message_refs", spy_extract)
    _append(_jsonl(iso), res)
    assert ai.catch_up(db_session) == {"demo"}  # incremental
    assert _refs(db_session) == [(K1, "artifact", m.id)]
    assert not any(k == "tool_use" for k, _ in extracted)  # came from the pending memo
    assert ai._artifact_pending == 0 and iso.armed == [1]


def test_pending_unavailable_rules(env, db_session, iso):
    # (a) no candidate file at all
    _tool_row(db_session, "toolu_test_a", agent="agentsec0001")
    # (b) the file exists but lacks the tool_use
    _append(_jsonl(iso, "s-fork"), {"type": "assistant", "message": {"content": []}})
    _tool_row(db_session, "toolu_test_b", agent="agentfork001", seconds=1)
    ai.catch_up(db_session)
    assert _memo(db_session, "toolu_test_a").outcome == "unavailable"
    assert _memo(db_session, "toolu_test_b").outcome == "unavailable"
    assert iso.armed == [] and ai._artifact_pending == 0

    # (c) tool_use found, result never written, call older than the TTL
    use, res = _page("toolu_test_c", A1)
    _append(_jsonl(iso), use)
    _tool_row(db_session, "toolu_test_c", seconds=2)
    ai.catch_up(db_session)
    assert _memo(db_session, "toolu_test_c").outcome == "pending" and iso.armed == [1]
    conn = ai._dbapi(db_session)
    row = dict(zip(xi._COLS, conn.execute(
        "SELECT %s FROM artifact_calls WHERE tool_use_id = ?" % ", ".join(xi._COLS),
        ("toolu_test_c",)).fetchone()))
    row["created_at"] = xi._db_ts(datetime.now(timezone.utc) - timedelta(hours=2))
    xi._upsert_calls(conn, [row])
    conn.commit()
    ai.catch_up(db_session)
    assert _memo(db_session, "toolu_test_c").outcome == "unavailable"
    assert ai._artifact_pending == 0 and iso.armed == [1]

    # (d) the transcript completes later: incremental runs leave it, a full run resolves it
    _append(_jsonl(iso), res)
    assert ai.catch_up(db_session) == set()
    assert _memo(db_session, "toolu_test_c").outcome == "unavailable"
    _force_full(db_session)
    assert ai.catch_up(db_session) == {"demo"}
    assert _memo(db_session, "toolu_test_c").outcome == "publish"
    assert K1 in {p for p, _, _ in _refs(db_session)}


def test_alias_dedupe_short_and_code_forms(env, db_session, iso):
    # create returns the short URL (no artifact_id); a later update returns the
    # short URL plus artifact_id; prose links the code form of that uuid.
    _append(_jsonl(iso), *_create("toolu_test_c", A1), *_update("toolu_test_u", A1, 2,
                                                                 artifact_id=UUID1))
    _tool_row(db_session, "toolu_test_c", seconds=1)
    _tool_row(db_session, "toolu_test_u", seconds=2)
    _msg(db_session, f"Deck updated: {CODE1}", seconds=3)
    ai.catch_up(db_session)

    assert {p for p, _, _ in _refs(db_session)} == {K1}
    row = _list(db_session)[K1]
    assert row["sources"] == ["artifact", "text"] and row["mention_count"] == 3
    assert row["artifact"]["publish_count"] == 2 and row["artifact"]["version"] == 2
    assert _memo(db_session, "toolu_test_u").artifact_key == K1

    # A later publish that only returns the code form joins the same key
    use, res = _update("toolu_test_v", CODE1, 3, artifact_id=UUID1)
    _append(_jsonl(iso), use, res)
    _tool_row(db_session, "toolu_test_v", seconds=4)
    ai.catch_up(db_session)
    assert {p for p, _, _ in _refs(db_session)} == {K1}
    art = _list(db_session)[K1]["artifact"]
    assert art["url"] == CODE1 and art["publish_count"] == 3  # latest form returned


def test_retitle_version_and_times(env, db_session, iso):
    for i, title in enumerate(("Old title", "Middle title", "New title"), 1):
        _append(_jsonl(iso), *_page(f"toolu_test_00{i}", A2, title=title, seq=i))
        _tool_row(db_session, f"toolu_test_00{i}", seconds=60 * i)
    ai.catch_up(db_session)
    art = _list(db_session)["artifact:" + A2]["artifact"]
    assert art["title"] == "New title" and art["version"] == 3
    assert art["publish_count"] == 3
    assert art["first_published_at"] < art["last_published_at"]


def test_subagent_workflow_transcript(env, db_session, iso):
    path = iso.tdir / "s-main" / "subagents" / "workflows" / "wf_test" / "agent-atest0001.jsonl"
    _append(str(path), *_page("toolu_test_001", A1))
    m = _tool_row(db_session, "toolu_test_001", agent="agentsub0001")
    ai.catch_up(db_session)
    assert _refs(db_session) == [(K1, "artifact", m.id)]
    art = _list(db_session)[K1]["artifact"]
    assert art["published_by_agent_id"] == "agentmain001"
    assert art["published_by_agent_name"] == "Main agent"


def test_rotated_session_via_cc_sessions(env, db_session, iso):
    db_session.add(CCSession(session_id="s-old", agent_id="agentmain001",
                             project_path=str(env["root"]), is_subagent_session=False,
                             started_at=_ts(-3600)))
    db_session.commit()
    use, res = _page("toolu_test_001", A1)
    _append(_jsonl(iso, "s-old"), use)
    m = _tool_row(db_session, "toolu_test_001")
    ai.catch_up(db_session)
    assert _memo(db_session, "toolu_test_001").outcome == "pending"
    _append(_jsonl(iso, "s-old"), res)
    assert ai.catch_up(db_session) == {"demo"}
    assert _refs(db_session) == [(K1, "artifact", m.id)]


def test_backup_cache_fallback(env, db_session, iso, tmp_path, monkeypatch):
    backup = tmp_path / "backup" / "session-cache"
    src_base = str(tmp_path / "claude" / "projects") + os.sep
    monkeypatch.setattr(xi, "_backup_twin", lambda p: str(backup / p[len(src_base):])
                        if p.startswith(src_base) else None)
    _append(str(backup / "demo" / "s-main.jsonl"), *_page("toolu_test_001", A1))
    assert not os.path.exists(_jsonl(iso))
    m = _tool_row(db_session, "toolu_test_001")
    ai.catch_up(db_session)
    assert _refs(db_session) == [(K1, "artifact", m.id)]


def test_metadata_survives_bump_after_jsonl_deleted(env, db_session, iso, monkeypatch):
    _append(_jsonl(iso), *_page("toolu_test_001", A1, title="Kept title"))
    _tool_row(db_session, "toolu_test_001")
    ai.catch_up(db_session)
    os.remove(_jsonl(iso))

    scans = []
    real_scan = xi.scan_transcripts
    monkeypatch.setattr(xi, "scan_transcripts", lambda plan: scans.append(plan) or real_scan(plan))
    monkeypatch.setattr(ai, "ASSET_INDEX_VERSION", ai.ASSET_INDEX_VERSION + 1)
    assert ai.catch_up(db_session) == {"demo"}
    assert scans == []
    assert db_session.query(ArtifactCall).count() == 1
    assert _list(db_session)[K1]["artifact"]["title"] == "Kept title"


def test_prose_rules(env, db_session, iso):
    _append(_jsonl(iso), *_page("toolu_test_001", A1))
    _tool_row(db_session, "toolu_test_001")
    # unknown URL in prose only: never creates an artifact
    _msg(db_session, f"someone else's page {A2}", seconds=1)
    # the demo key mentioned from another project: not listed there
    _msg(db_session, f"see {A1}", agent="agentoth0001", seconds=2)
    # a #fragment mention counts in demo
    fm = _msg(db_session, f"slide two: {A1}#slide-2", seconds=3)
    ai.catch_up(db_session)

    assert {p for p, _, _ in _refs(db_session)} == {K1}
    assert (K1, "text", fm.id) in _refs(db_session)
    assert _refs(db_session, "other") == []
    assert _list(db_session, "other") == {}


def test_deep_link_publish_row(env, db_session, iso):
    _append(_jsonl(iso), *_page("toolu_test_001", A1))
    pub = _tool_row(db_session, "toolu_test_001")
    _msg(db_session, f"Here it is: {A1}", seconds=30)
    _msg(db_session, f"Also see {A1}", agent="agentsec0001", seconds=60)
    ai.catch_up(db_session)
    row = _list(db_session)[K1]
    assert row["mention_count"] == 3
    # The publish row beats later prose mentions at the same tier
    assert (row["last_message_id"], row["last_agent_id"]) == (pub.id, "agentmain001")


def test_failed_and_asset_calls_ignored(env, db_session, iso):
    _append(
        _jsonl(iso),
        _use("toolu_test_e1", {"file_path": "a.html"}),
        _result("toolu_test_e1", "favicon required",
                "<tool_use_error>favicon required to publish</tool_use_error>", is_error=True),
        _use("toolu_test_e2", {"url": A1, "files": ["a"], "root": "r"}),
        _result("toolu_test_e2", text="<tool_use_error>also pass file_path</tool_use_error>"),
        _use("toolu_test_as", {"url": A1, "asset": True, "file_path": "a.png"}),
        _result("toolu_test_as", {"written": {"url": A1}, "asset_upload": {"id": "x"}},
                f"Asset uploaded: a.png is now stored in {A1}"),
    )
    for i, tid in enumerate(("toolu_test_e1", "toolu_test_e2", "toolu_test_as")):
        _tool_row(db_session, tid, seconds=i)
    ai.catch_up(db_session)
    assert _refs(db_session) == [] and _list(db_session) == {}
    assert [_memo(db_session, t).outcome for t in ("toolu_test_e1", "toolu_test_e2",
                                                    "toolu_test_as")] == ["error", "error", "asset"]


def test_deleted_artifact(env, db_session, iso):
    _append(_jsonl(iso), *_page("toolu_test_001", A1),
            _use("toolu_test_del", {"action": "delete", "url": A1}),
            _result("toolu_test_del", {"deleted": {"url": A1}}, f"Deleted {A1}"))
    _tool_row(db_session, "toolu_test_001")
    _tool_row(db_session, "toolu_test_del", seconds=60)
    ai.catch_up(db_session)
    assert _memo(db_session, "toolu_test_del").outcome == "deleted"
    assert K1 not in _list(db_session)
    row = _list(db_session, include_missing=True)[K1]
    assert row["exists"] is False and row["artifact"]["deleted"] is True
    assert row["artifact"]["publish_count"] == 1


def test_refless_marks(env, db_session, iso):
    _append(_jsonl(iso), *_page("toolu_test_001", A1, title="Starred page"))
    _tool_row(db_session, "toolu_test_001")
    ai.catch_up(db_session)
    ai.set_marks(db_session, "demo", [K1], starred=True)
    db_session.query(AssetRef).delete()
    db_session.commit()

    row = _list(db_session)[K1]
    assert row["sources"] == [] and row["kind"] == "artifact" and row["starred"]
    assert row["artifact"]["title"] == "Starred page"
    assert row["artifact"]["published_by_agent_name"] == "Main agent"

    # The same key starred in a project that never published it: key-only object
    ai.set_marks(db_session, "other", [K1], starred=True)
    art = _list(db_session, "other")[K1]["artifact"]
    assert art["url"] == A1 and art["title"] is None and art["kind"] == "other"
    assert art["published_by_agent_name"] is None

    # Junk keys are never listed
    junk = ["artifact:javascript:x", "artifact:https://example.com/a", "artifact:" + A1 + "?x=1"]
    ai.set_marks(db_session, "demo", junk, starred=True)
    assert not set(junk) & set(_list(db_session))


def test_prefilters():
    assert ai.turns_may_have_assets([("assistant", "> `Artifact`",
                                      {"tool_name": "Artifact", "tool_use_id": "t"},
                                      None, "tool_use", None)])
    assert ai.turns_may_have_assets([("assistant", f"link: {A1}", None, None, "text", None)])
    assert not ai.turns_may_have_assets([("assistant", "> `Bash`",
                                          {"tool_name": "Bash", "tool_use_id": "t"},
                                          None, "tool_use", None)])
    assert ai.content_may_have_assets(f"Published: {A1}")
    assert ai.extract_text_paths(f"see {A1}") == []
    assert ai.extract_message_refs("text", f"see {A1} and out/a.png", None) == [
        ("text", "out/a.png", False, None), ("text", A1, False, None)]
    assert ai.extract_message_refs(
        "tool_use", "> `Artifact`", json.dumps({"tool_name": "Artifact", "tool_use_id": "t9"})
    ) == [("artifact", "t9", False, None)]
    assert ai.extract_message_refs("tool_use", "> `Artifact`", None) == []


@pytest.fixture()
def _worker_session(monkeypatch, db_engine):
    """catch_up() with no db opens database.SessionLocal: point it at the test engine."""
    import database
    from sqlalchemy.orm import sessionmaker
    monkeypatch.setattr(database, "SessionLocal",
                        sessionmaker(bind=db_engine, autoflush=False, expire_on_commit=False))



@pytest.mark.anyio
async def test_api_lists_artifact_row(client, env, db_session, iso, _worker_session):
    _append(_jsonl(iso), *_page("toolu_test_001", A1, title="API page"))
    _tool_row(db_session, "toolu_test_001")

    # GET does no transcript I/O: the call stays pending for the worker
    resp = await client.get("/api/projects/demo/assets")
    assert resp.status_code == 200 and resp.json()["assets"] == []
    assert iso.armed == [1]
    ai.catch_up()  # the worker run (own session on the test engine below)

    resp = await client.get("/api/projects/demo/assets")
    row = _by_path(resp.json())[K1]
    assert row["kind"] == "artifact" and row["artifact"]["title"] == "API page"
    assert row["artifact"]["url"] == A1

    resp = await client.post("/api/projects/demo/assets/marks",
                             json={"paths": [K1], "starred": True})
    assert resp.status_code == 200
    assert resp.json()["marks"][K1]["starred"] is True
    resp = await client.get("/api/projects/demo/assets")
    assert _by_path(resp.json())[K1]["starred"] is True


def test_fork_copies_share_tool_use_id(env, db_session, iso, monkeypatch):
    use, res = _page("toolu_test_001", A1)
    _append(_jsonl(iso, "s-main"), use)
    m1 = _tool_row(db_session, "toolu_test_001")
    ai.catch_up(db_session)
    assert _memo(db_session, "toolu_test_001").outcome == "pending"

    m2 = _tool_row(db_session, "toolu_test_001", agent="agentfork001", seconds=5)
    _append(_jsonl(iso, "s-fork"), use, res)
    os.remove(_jsonl(iso, "s-main"))
    assert ai.catch_up(db_session) == {"demo"}  # incremental, via the fork's transcript
    assert _refs(db_session) == sorted([(K1, "artifact", m1.id), (K1, "artifact", m2.id)])
    art = _list(db_session)[K1]["artifact"]
    assert art["publish_count"] == 1 and art["published_by_agent_id"] == "agentmain001"
    assert _memo(db_session, "toolu_test_001").message_id == m1.id

    scans = []
    real_scan = xi.scan_transcripts
    monkeypatch.setattr(xi, "scan_transcripts", lambda plan: scans.append(plan) or real_scan(plan))
    m3 = _tool_row(db_session, "toolu_test_001", agent="agentsec0001", seconds=9)
    assert ai.catch_up(db_session) == {"demo"}
    assert (K1, "artifact", m3.id) in _refs(db_session)
    assert scans == []


def test_transcript_errors_are_isolated(env, db_session, iso):
    tid = "toolu_test_001"
    use, res = _page(tid, A1)
    path = _jsonl(iso)
    _append(path,
            "{not json " + tid,
            {"type": "assistant", "message": {"content": f"a string mentioning {tid}"}},
            {"type": "assistant", "message": {"content": [f"bare {tid}", 7, None]}},
            {"type": "assistant", "message": "not a dict " + tid},
            use)
    with open(path, "a") as f:
        f.write(json.dumps(res))  # truncated: no trailing newline yet
    _tool_row(db_session, tid)
    _msg(db_session, "chart in out/plot.png", seconds=1)

    assert ai.catch_up(db_session) == {"demo"}
    assert [(p, s) for p, s, _ in _refs(db_session)] == [("out/plot.png", "text")]
    state = json.loads(db_session.get(SystemConfig, ai.STATE_KEY).value)
    max_rowid = ai._dbapi(db_session).execute("SELECT MAX(rowid) FROM messages").fetchone()[0]
    assert state["rowid"] == max_rowid
    assert _memo(db_session, tid).outcome == "pending"

    with open(path, "a") as f:
        f.write("\n")
    assert ai.catch_up(db_session) == {"demo"}
    assert _memo(db_session, tid).outcome == "publish"
    assert (K1, "artifact") in {(p, s) for p, s, _ in _refs(db_session)}


def test_artifact_pass_failure_keeps_file_refs(env, db_session, iso, monkeypatch):
    _append(_jsonl(iso), *_page("toolu_test_001", A1))
    _tool_row(db_session, "toolu_test_001")
    _msg(db_session, "chart in out/plot.png", seconds=1)

    def boom(*a, **kw):
        raise RuntimeError("resolver exploded")

    real_resolve = xi.resolve_calls
    monkeypatch.setattr(xi, "resolve_calls", boom)
    assert ai.catch_up(db_session) == {"demo"}
    assert [(p, s) for p, s, _ in _refs(db_session)] == [("out/plot.png", "text")]
    assert _memo(db_session, "toolu_test_001").outcome == "pending"

    monkeypatch.setattr(xi, "resolve_calls", real_resolve)
    assert ai.catch_up(db_session) == {"demo"}  # incremental: the stub is still in the work set
    assert _memo(db_session, "toolu_test_001").outcome == "publish"
    assert (K1, "artifact") in {(p, s) for p, s, _ in _refs(db_session)}

    # One malformed result: that call is ignored, the run goes on
    def bad_shape(*a, **kw):
        raise TypeError("unexpected shape")

    monkeypatch.setattr(xi, "classify_call", bad_shape)
    _append(_jsonl(iso), *_page("toolu_test_002", A2))
    _tool_row(db_session, "toolu_test_002", seconds=2)
    ai.catch_up(db_session)
    assert _memo(db_session, "toolu_test_002").outcome == "ignored"
    assert "artifact:" + A2 not in {p for p, _, _ in _refs(db_session)}


def test_mention_failure_is_isolated(env, db_session, iso, monkeypatch):
    def boom(*a, **kw):
        raise RuntimeError("mentions exploded")

    monkeypatch.setattr(xi, "mention_refs", boom)
    _append(_jsonl(iso), *_page("toolu_test_001", A1))
    _tool_row(db_session, "toolu_test_001")
    _msg(db_session, f"chart in out/plot.png, page {A1}", seconds=1)
    assert ai.catch_up(db_session) == {"demo"}
    assert {(p, s) for p, s, _ in _refs(db_session)} == {("out/plot.png", "text"),
                                                         (K1, "artifact")}


def test_retry_timer_backoff_and_reset(monkeypatch):
    fired = threading.Event()
    calls = []

    def recorder():
        calls.append(1)
        fired.set()

    monkeypatch.setattr(ai, "_arm_artifact_retry", _real_arm_artifact_retry)
    monkeypatch.setattr(ai, "_ARTIFACT_RETRY_DELAYS", (0.01, 0.02))
    monkeypatch.setattr(ai, "schedule_catch_up", recorder)

    def wait_fired():
        assert fired.wait(1.0)
        fired.clear()
        deadline = time.monotonic() + 1.0
        while ai._artifact_timer is not None and time.monotonic() < deadline:
            time.sleep(0.005)
        assert ai._artifact_timer is None

    try:
        ai._on_artifact_pending(1)
        wait_fired()
        assert ai._artifact_retry_idx == 1 and calls == [1]
        ai._on_artifact_pending(1)
        wait_fired()
        assert ai._artifact_retry_idx == 2 and calls == [1, 1]  # delay clamped to the last
        ai._on_artifact_pending(0)
        assert ai._artifact_retry_idx == 0 and ai._artifact_timer is None

        # While a timer is armed, more pending runs don't stack timers or steps
        monkeypatch.setattr(ai, "_ARTIFACT_RETRY_DELAYS", (5,))
        ai._on_artifact_pending(2)
        timer = ai._artifact_timer
        ai._on_artifact_pending(2)
        assert ai._artifact_timer is timer and ai._artifact_retry_idx == 1
        ai._on_artifact_pending(0)
        assert ai._artifact_timer is None and ai._artifact_retry_idx == 0
        timer.join(1.0)
        assert not timer.is_alive() and calls == [1, 1]  # cancelled, never fired
    finally:
        t = ai._artifact_timer
        if t is not None:
            t.cancel()


def test_interactive_run_does_no_transcript_io(env, db_session, iso, monkeypatch):
    _append(_jsonl(iso), *_page("toolu_test_001", A1))
    m = _tool_row(db_session, "toolu_test_001")
    reads = []
    real_scan, real_resolve = xi.scan_transcripts, xi._resolve_jsonl
    monkeypatch.setattr(xi, "scan_transcripts", lambda plan: reads.append(plan) or real_scan(plan))
    monkeypatch.setattr(xi, "_resolve_jsonl", lambda *a, **kw: reads.append(a) or real_resolve(*a))

    assert ai.catch_up(db_session, interactive=True) == set()
    assert reads == []
    assert _memo(db_session, "toolu_test_001").outcome == "pending"
    assert iso.armed == [1] and _refs(db_session) == []

    assert ai.catch_up(db_session) == {"demo"}  # the worker run the timer hands it to
    assert reads and _refs(db_session) == [(K1, "artifact", m.id)]

    # A version bump rebuilt by a GET serves the memo with no transcript read
    reads.clear()
    monkeypatch.setattr(ai, "ASSET_INDEX_VERSION", ai.ASSET_INDEX_VERSION + 1)
    assert ai.catch_up(db_session, interactive=True) == {"demo"}
    assert reads == [] and _refs(db_session) == [(K1, "artifact", m.id)]


def test_mention_scanned_before_publish_is_backfilled(env, db_session, iso):
    # A GET run sees the tool row and the prose link together but resolves
    # nothing, so the mention is dropped and the watermark moves past it.
    _append(_jsonl(iso), *_page("toolu_test_001", A1))
    _tool_row(db_session, "toolu_test_001")
    prose = _msg(db_session, f"Shared at {A1}", seconds=5)
    ai.catch_up(db_session, interactive=True)
    assert _refs(db_session) == []

    assert ai.catch_up(db_session) == {"demo"}  # worker: resolves, then backfills the mention
    assert (K1, "text", prose.id) in _refs(db_session)
    assert _list(db_session)[K1]["sources"] == ["artifact", "text"]


@pytest.mark.filterwarnings("error::DeprecationWarning")
def test_memo_rows_raw_format(env, db_session, iso):
    _append(_jsonl(iso), *_page("toolu_test_001", A1))
    _tool_row(db_session, "toolu_test_001")
    ai.catch_up(db_session)
    raw = ai._dbapi(db_session).execute(
        "SELECT indexed_at, created_at, extract_version FROM artifact_calls").fetchone()
    stamp = re.compile(r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\.\d{6}$")
    assert stamp.match(raw[0]) and stamp.match(raw[1])
    assert raw[2] == xi.ARTIFACT_EXTRACT_VERSION == 1
    row = _memo(db_session, "toolu_test_001")
    assert row.created_at.tzinfo is None and row.indexed_at.tzinfo is None
    assert row.created_at == _ts(0)


def test_interactive_full_run_leaves_artifact_pass_owed(env, db_session, iso, monkeypatch):
    # Index once: one publish, one call with no transcript (unavailable).
    _append(_jsonl(iso), *_page("toolu_test_001", A1))
    _tool_row(db_session, "toolu_test_001")
    _tool_row(db_session, "toolu_test_002", agent="agentsec0001", seconds=1)
    ai.catch_up(db_session)
    assert _memo(db_session, "toolu_test_002").outcome == "unavailable"
    assert iso.armed == []

    # A classify fix (both versions bumped) and the missing transcript is back.
    # The GET wins the full rebuild: it reads nothing, so the full artifact
    # pass stays owed and the timer hands it to the worker.
    _append(_jsonl(iso, "s-sec"), *_page("toolu_test_002", A2))
    monkeypatch.setattr(xi, "ARTIFACT_EXTRACT_VERSION", 2)
    monkeypatch.setattr(ai, "ASSET_INDEX_VERSION", ai.ASSET_INDEX_VERSION + 1)
    ai.catch_up(db_session, interactive=True)
    assert _memo(db_session, "toolu_test_001").extract_version == 1
    assert iso.armed == [1]
    state = json.loads(db_session.get(SystemConfig, ai.STATE_KEY).value)
    assert state["artifact_full"] is True

    assert ai.catch_up(db_session) == {"demo"}  # incremental, but does the owed pass
    assert _memo(db_session, "toolu_test_001").extract_version == 2
    assert _memo(db_session, "toolu_test_002").outcome == "publish"
    assert ("artifact:" + A2, "artifact") in {(p, s) for p, s, _ in _refs(db_session)}
    db_session.expire_all()
    assert "artifact_full" not in json.loads(db_session.get(SystemConfig, ai.STATE_KEY).value)
    assert ai._artifact_pending == 0


def test_resolve_failure_keeps_retry_armed(env, db_session, iso, monkeypatch):
    _msg(db_session, "chart in out/plot.png")
    ai.catch_up(db_session)  # full run; the next one is incremental
    assert iso.armed == []

    def boom(*a, **kw):
        raise RuntimeError("resolver exploded")

    monkeypatch.setattr(xi, "resolve_calls", boom)
    _append(_jsonl(iso), *_page("toolu_test_001", A1))
    _tool_row(db_session, "toolu_test_001", seconds=1)
    ai.catch_up(db_session)
    assert _memo(db_session, "toolu_test_001").outcome == "pending"
    # The stub is still pending: the retry stays armed, not reset
    assert ai._artifact_pending == 1 and iso.armed == [1]


def test_mention_before_late_subagent_row_is_backfilled(env, db_session, iso):
    # The parent's prose links the artifact before the subagent's tool row is
    # imported (late subagent import): the prose has the lower rowid.
    path = iso.tdir / "s-main" / "subagents" / "agent-atest0001.jsonl"
    _append(str(path), *_page("toolu_test_001", A1))
    prose = _msg(db_session, f"The subagent published {A1}")
    ai.catch_up(db_session)  # key unknown yet: the mention is dropped
    assert _refs(db_session) == []

    _tool_row(db_session, "toolu_test_001", agent="agentsub0001", seconds=-5)
    assert ai.catch_up(db_session) == {"demo"}
    assert (K1, "text", prose.id) in _refs(db_session)
    row = _list(db_session)[K1]
    assert row["sources"] == ["artifact", "text"]
    # The top-level prose now takes the deep link over the subagent's row
    assert (row["last_message_id"], row["last_agent_id"]) == (prose.id, "agentmain001")
    assert _refs(db_session, "other") == []


def test_update_by_url_joins_the_key_it_names(env, db_session, iso):
    # The create returns the short form with no artifact_id; the update names
    # that short URL as input.url but returns only the code form + artifact_id.
    use, res = _update("toolu_test_u", CODE1, 2, artifact_id=UUID1)
    use["message"]["content"][0]["input"]["url"] = A1
    _append(_jsonl(iso), *_create("toolu_test_c", A1), use, res)
    _tool_row(db_session, "toolu_test_c", seconds=1)
    _tool_row(db_session, "toolu_test_u", seconds=2)
    _msg(db_session, f"Deck v2: {CODE1}", seconds=3)
    ai.catch_up(db_session)

    assert {p for p, _, _ in _refs(db_session)} == {K1}
    assert _memo(db_session, "toolu_test_u").artifact_key == K1
    assert _memo(db_session, "toolu_test_u").url == CODE1  # the alias is never stored as url
    row = _list(db_session)[K1]
    assert row["sources"] == ["artifact", "text"]
    assert row["artifact"]["publish_count"] == 2 and row["artifact"]["url"] == CODE1

    # A type URL passed as input.url never becomes an alias
    c = xi.classify_call({"url": TYPE_SLIDES, "file_path": "a.html"},
                         {"is_error": False, "text": f"Updated the Artifact at {A2}",
                          "toolUseResult": {"url": A2}}, {TYPE_SLIDES: "Slides"})
    assert (c["outcome"], c["target_url"]) == ("publish", None)
