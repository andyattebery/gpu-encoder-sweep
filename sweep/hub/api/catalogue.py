"""sweep/hub/api/catalogue.py -- the catalogue verbs: one POST per FILE table group, each one checked transaction."""
from fastapi import APIRouter, Depends

from sweep.hub import store as st
from sweep.hub.api.common import OK, Body, get_store

router = APIRouter(prefix="/catalogue", tags=["catalogue"])


class AddHost(Body):
    host: str
    ssh_host: str
    os: str
    machine: str
    work_root: str
    share_root: str
    ffmpeg: str | None = None
    local_view: str | None = None
    notes: str | None = None


@router.post("/add-host")
def add_host(body: AddHost, store=Depends(get_store)):
    with store.transaction() as conn:
        st.insert(conn, "host", dict(body.model_dump(), blocked=None))
    return OK
