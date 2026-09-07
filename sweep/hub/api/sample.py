"""sweep/hub/api/sample.py -- the sample verbs: a pinned window, a classified cut check, a content class with its frame."""
from fastapi import APIRouter, Depends

from sweep.hub import store as st
from sweep.hub.api.common import OK, Body, get_store

router = APIRouter(prefix="/sample", tags=["sample"])


class PinWindow(Body):
    window_id: str
    title_id: str
    ss: float
    t: float
    character: str | None = None
    selected_by: str | None = None
    selection_score: float | None = None
    notes: str | None = None


@router.post("/pin-window")
def pin_window(body: PinWindow, store=Depends(get_store)):
    """A window is (title, ss, t), pinned by a person; it is never re-scanned."""
    with store.transaction() as conn:
        st.require(conn, "title", title_id=body.title_id)
        st.insert(conn, "window", dict(body.model_dump(), origin="pinned"))
    return OK


class ClassifyCut(Body):
    cut_id: str
    check_name: str
    reason: str
    checked_at: str


@router.post("/classify-cut")
def classify_cut(body: ClassifyCut, store=Depends(get_store)):
    """A content check a person classifies as acceptable, with the reason; the cut is then in use."""
    with store.transaction() as conn:
        st.insert_cut_check(conn, body.cut_id, body.check_name, "classified", body.reason, body.checked_at)
    return OK


class Stratum(Body):
    stratum: str
    kind: str
    definition: str
    min_windows: int = 1
    share_estimate: float | None = None


class DefineClass(Body):
    content_class_id: str
    name: str
    reference_set_id: str
    description: str | None = None
    lanes: list[str]
    members: list[str]
    strata: list[Stratum]


@router.post("/define-class")
def define_class(body: DefineClass, store=Depends(get_store)):
    """A content class: its reference set, the lanes it serves, its members and the frame it was picked by."""
    with store.transaction() as conn:
        st.require(conn, "reference_set", reference_set_id=body.reference_set_id)
        st.insert(conn, "content_class", {"content_class_id": body.content_class_id, "name": body.name,
                                          "reference_set_id": body.reference_set_id, "description": body.description})
        for lane in body.lanes:
            st.require(conn, "lane", lane=lane)
            st.insert(conn, "content_class_lane", {"content_class_id": body.content_class_id, "lane": lane})
        for window in body.members:
            st.require(conn, "window", window_id=window)
            st.insert(conn, "content_class_member", {"content_class_id": body.content_class_id, "window_id": window})
        for s in body.strata:
            st.insert(conn, "content_class_stratum", dict(content_class_id=body.content_class_id, **s.model_dump()))
    return OK
