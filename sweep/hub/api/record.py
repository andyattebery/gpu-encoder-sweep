"""sweep/hub/api/record.py -- the record as files: what `sweep export --into DIR` writes into the campaign repo."""
from fastapi import APIRouter, Depends, Request

from sweep.hub import export
from sweep.hub.api.common import get_store

router = APIRouter(prefix="/record", tags=["record"])


@router.get("/export")
def export_record(request: Request, store=Depends(get_store)):
    with store.reading() as conn:
        files = export.render(conn, request.app.openapi())
    return {"files": dict(files)}
