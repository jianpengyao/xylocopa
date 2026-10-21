"""Raw file routes (/api/files, /api/thumbs fallback, /api/uploads): active
content (HTML / SVG / XML) is served with "Content-Security-Policy: sandbox"
and nosniff; PDFs, images and videos are not sandboxed."""

import pytest
from sqlalchemy.orm import sessionmaker

from models import Project

SANDBOXED = {
    "page.html": "<script>alert(1)</script>",
    "page.htm": "<script>alert(1)</script>",
    "doc.xhtml": "<html xmlns='http://www.w3.org/1999/xhtml'/>",
    "img.svg": "<svg xmlns='http://www.w3.org/2000/svg'><script>alert(1)</script></svg>",
    "data.xml": "<a/>",
}
PLAIN = {
    "doc.pdf": "%PDF-1.4\n",
    "photo.png": "\x89PNG\r\n\x1a\n",
    "clip.webm": "\x1aE\xdf\xa3",
    "notes.txt": "hello",
    "app.js": "alert(1)",
}


@pytest.fixture()
def raw_project(db_engine, tmp_path):
    Session = sessionmaker(bind=db_engine, autoflush=False, expire_on_commit=False)
    db = Session()
    db.add(Project(name="raw-proj", display_name="Raw", path=str(tmp_path),
                   max_concurrent=2, default_model="claude-opus-4-7"))
    db.commit()
    db.close()
    uploads = tmp_path / ".xylocopa" / "uploads"
    uploads.mkdir(parents=True)
    for name, content in {**SANDBOXED, **PLAIN}.items():
        (tmp_path / name).write_bytes(content.encode("latin-1"))
        (uploads / f"u1_{name}").write_bytes(content.encode("latin-1"))
    return tmp_path


def _assert_sandboxed(resp):
    assert resp.status_code == 200
    assert resp.headers["content-security-policy"] == "sandbox"
    assert resp.headers["x-content-type-options"] == "nosniff"


def _assert_plain(resp):
    assert resp.status_code == 200
    assert "content-security-policy" not in resp.headers


@pytest.mark.anyio
@pytest.mark.parametrize("name", sorted(SANDBOXED))
async def test_active_content_sandboxed(client, raw_project, name):
    _assert_sandboxed(await client.get(f"/api/files/raw-proj/{name}"))
    _assert_sandboxed(await client.get(f"/api/files/raw-proj/{name}?download=1"))
    _assert_sandboxed(await client.get(f"/api/thumbs/raw-proj/{name}"))
    _assert_sandboxed(await client.get(f"/api/uploads/u1_{name}"))


@pytest.mark.anyio
@pytest.mark.parametrize("name", sorted(PLAIN))
async def test_pdf_images_video_not_sandboxed(client, raw_project, name):
    _assert_plain(await client.get(f"/api/files/raw-proj/{name}"))
    _assert_plain(await client.get(f"/api/uploads/u1_{name}"))
    if name.endswith(".pdf"):
        _assert_plain(await client.get(f"/api/thumbs/raw-proj/{name}"))


@pytest.mark.anyio
async def test_download_keeps_attachment_disposition(client, raw_project):
    resp = await client.get("/api/files/raw-proj/page.html?download=1")
    _assert_sandboxed(resp)
    assert resp.headers["content-disposition"].startswith("attachment")
    assert resp.content == SANDBOXED["page.html"].encode()
