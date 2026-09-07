"""sweep/hub/api/decision.py -- the decision verbs: a person's viewing, what ships, and where a lane is not routed."""
from fastapi import APIRouter, Depends

from sweep.hub import store as st
from sweep.hub.api.common import OK, Body, get_store

router = APIRouter(prefix="/decision", tags=["decision"])


class RecordViewing(Body):
    kind: str                      # pair | acceptance
    lane: str | None = None        # an acceptance names the lane whose use it judges
    window_id: str
    cell_a: str
    cell_b: str | None = None      # a pair's second encode
    viewed_on: str                 # the device (the column's name is a reserved word for a free input)
    viewer: str
    verdict: str
    notes: str | None = None
    viewed_at: str


@router.post("/record-viewing")
def record_viewing(body: RecordViewing, store=Depends(get_store)):
    """A person's verdict on the device, on encodes that were KEPT; the id names it in a search's targets or incumbent."""
    with store.transaction() as conn:
        st.require(conn, "window", window_id=body.window_id)
        st.require(conn, "cell", cell_key=body.cell_a)
        if body.cell_b is not None:
            st.require(conn, "cell", cell_key=body.cell_b)
        if body.lane is not None:
            st.require(conn, "lane", lane=body.lane)
        row = body.model_dump()
        row["device"] = row.pop("viewed_on")
        viewing_id = st.insert_returning_id(conn, "viewing_verdict", row, "viewing_id")
    return {"viewing_id": viewing_id}


class ShipSetting(Body):
    setting_id: str
    value: str
    role: str                      # identity | computed | default_resolved
    from_constant: str | None = None


class ShipRow(Body):
    step: str
    encoder_unit_id: str | None = None
    provenance: str
    decided_by: str
    reason: str | None = None
    content_class_id: str | None = None
    workers: int | None = None
    evidence_query: str | None = None
    cites_json: str | None = None
    settings: list[ShipSetting] = []


class Ship(Body):
    lane: str
    host: str
    rows: list[ShipRow]            # every step of the lane on that host, in one call: routing is complete at every moment


@router.post("/ship")
def ship(body: Ship, store=Depends(get_store)):
    ids = []
    with store.transaction() as conn:
        st.require(conn, "lane", lane=body.lane)
        st.require(conn, "host", host=body.host)
        for row in body.rows:
            st.require(conn, "lane_step", lane=body.lane, step=row.step)
            if row.encoder_unit_id is not None:
                st.require(conn, "encoder_unit", encoder_unit_id=row.encoder_unit_id)
            if row.content_class_id is not None:
                st.require(conn, "content_class", content_class_id=row.content_class_id)
            shipped = dict(lane=body.lane, host=body.host, **row.model_dump(exclude={"settings"}))
            shipped_id = st.insert_returning_id(conn, "shipped", shipped, "shipped_id")
            for s in row.settings:
                st.require(conn, "setting", setting_id=s.setting_id)
                if s.from_constant is not None:
                    st.require(conn, "constant", name=s.from_constant)
                st.insert(conn, "shipped_setting", dict(shipped_id=shipped_id, **s.model_dump()))
            ids.append(shipped_id)
    return {"shipped_ids": ids}


class ExcludeRoute(Body):
    lane: str
    host: str
    reason: str


@router.post("/exclude-route")
def exclude_route(body: ExcludeRoute, store=Depends(get_store)):
    """A host a lane is deliberately not routed to, with the reason; the completeness rule reads it."""
    with store.transaction() as conn:
        st.require(conn, "lane", lane=body.lane)
        st.require(conn, "host", host=body.host)
        st.insert(conn, "routing_exclusion", body.model_dump())
    return OK
