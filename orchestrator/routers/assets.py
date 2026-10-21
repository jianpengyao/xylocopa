"""Project Assets — files agents surfaced in chat (see asset_index.py).

GET  /api/projects/{name}/assets        aggregated asset list (+ catch-up)
POST /api/projects/{name}/assets/marks  batch star / hide / note
"""

import asyncio
import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

import asset_index
from database import get_db
from route_helpers import get_project_or_404
from utils import utcnow

logger = logging.getLogger(__name__)

router = APIRouter(tags=["assets"])

_MAX_PATH_LEN = 4096
_MAX_NOTE_LEN = 10_000


class AssetMarksUpdate(BaseModel):
    paths: list[str] = Field(..., min_length=1, max_length=1000)
    starred: bool | None = None
    hidden: bool | None = None
    note: str | None = Field(None, max_length=_MAX_NOTE_LEN)


@router.get("/api/projects/{name}/assets")
async def list_project_assets(name: str, include_missing: bool = False,
                              db: Session = Depends(get_db)):
    """Assets for a project. Catches the index up first, so the list is
    always fresh. Missing files are omitted unless include_missing=1
    (starred ones are always kept, flagged exists=false)."""

    def _work():
        get_project_or_404(db, name)
        try:
            changed = asset_index.catch_up(db)
        except Exception:  # noqa: BLE001 — serve the current index, not a 500
            logger.warning("asset_index: catch_up failed; listing current index",
                           exc_info=True)
            changed = set()
        out = asset_index.list_assets(db, name, include_missing=include_missing)
        out["indexed_at"] = asset_index.iso_utc(utcnow())
        return changed, out

    changed, out = await asyncio.to_thread(_work)
    if changed:
        # Refs this request indexed won't be seen as new by the scheduled
        # worker, so announce them here or other open clients stay stale.
        from websocket import emit_assets_changed
        for p in sorted(changed):
            asyncio.ensure_future(emit_assets_changed(p))
        asset_index.schedule_tile_warm(changed)
    return out


@router.post("/api/projects/{name}/assets/marks")
async def update_asset_marks(name: str, body: AssetMarksUpdate,
                             db: Session = Depends(get_db)):
    """Batch star / hide / note for asset paths. null = unchanged; note ""
    clears it. Emits WS asset_marks_changed with only the fields set."""
    paths = [p for p in body.paths if isinstance(p, str) and p.strip()]
    if not paths:
        raise HTTPException(400, "paths must contain at least one non-empty path")
    if any(len(p) > _MAX_PATH_LEN for p in paths):
        raise HTTPException(400, "path too long")
    if body.starred is None and body.hidden is None and body.note is None:
        raise HTTPException(400, "nothing to update: set starred, hidden or note")

    def _work():
        get_project_or_404(db, name)
        return asset_index.set_marks(
            db, name, paths,
            starred=body.starred, hidden=body.hidden, note=body.note,
        )

    marks = await asyncio.to_thread(_work)

    fields = {}
    if body.starred is not None:
        fields["starred"] = body.starred
    if body.hidden is not None:
        fields["hidden"] = body.hidden
    if body.note is not None:
        fields["note"] = body.note.strip() or None
    from websocket import emit_asset_marks_changed
    asyncio.ensure_future(emit_asset_marks_changed(name, list(marks), **fields))
    return {"marks": marks}
