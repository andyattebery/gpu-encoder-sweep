"""sweep/hub/api/search.py -- the search verbs: the spec of a measurement, and the arm that ships."""
from fastapi import APIRouter, Depends

from sweep.hub import store as st
from sweep.hub.api.common import OK, Body, get_store

router = APIRouter(prefix="/search", tags=["search"])


class ArmSetting(Body):
    setting_id: str
    value: str


class Arm(Body):
    arm_id: str
    name: str
    role: str
    anchor_value: str | None = None            # the incumbent is pinned; the base and the candidates sweep the anchor
    accepted_by_viewing: int | None = None     # the incumbent names the acceptance viewing
    settings: list[ArmSetting]


class Target(Body):
    metric: str
    statistic: str
    target: float
    viewing_id: int | None = None


class AuthorSearch(Body):
    search_id: str
    content_class_id: str
    encoder_unit_id: str
    anchor_setting_id: str
    score_height: int
    notes: str | None = None
    arms: list[Arm]
    coarse_rungs: list[int] = []
    targets: list[Target] = []


@router.post("/author-search")
def author_search(body: AuthorSearch, store=Depends(get_store)):
    """The spec: class, unit, anchor, height, arms with their settings, the coarse ladder and the targets."""
    with store.transaction() as conn:
        st.require(conn, "content_class", content_class_id=body.content_class_id)
        st.require(conn, "encoder_unit", encoder_unit_id=body.encoder_unit_id)
        st.require(conn, "setting", setting_id=body.anchor_setting_id)
        st.insert(conn, "search", {"search_id": body.search_id, "content_class_id": body.content_class_id,
                                   "encoder_unit_id": body.encoder_unit_id, "anchor_setting_id": body.anchor_setting_id,
                                   "score_height": body.score_height, "notes": body.notes, "shipping_arm_id": None})
        for arm in body.arms:
            if arm.accepted_by_viewing is not None:
                st.require(conn, "viewing_verdict", viewing_id=arm.accepted_by_viewing)
            st.insert(conn, "arm", {"arm_id": arm.arm_id, "search_id": body.search_id, "name": arm.name, "role": arm.role,
                                    "accepted_by_viewing": arm.accepted_by_viewing, "anchor_value": arm.anchor_value})
            for s in arm.settings:
                st.require(conn, "setting", setting_id=s.setting_id)
                st.insert(conn, "arm_setting", {"arm_id": arm.arm_id, "setting_id": s.setting_id, "value": s.value})
        for rung in body.coarse_rungs:
            st.insert(conn, "search_coarse_rung", {"search_id": body.search_id, "rung": rung})
        for t in body.targets:
            if t.viewing_id is not None:
                st.require(conn, "viewing_verdict", viewing_id=t.viewing_id)
            st.insert(conn, "search_target", dict(search_id=body.search_id, **t.model_dump()))
    return OK


class SetShippingArm(Body):
    search_id: str
    arm_id: str


@router.post("/set-shipping-arm")
def set_shipping_arm(body: SetShippingArm, store=Depends(get_store)):
    """The arm Stage 10 inverts at; naming it is when the ladder and the incumbent's bar must be complete."""
    with store.transaction() as conn:
        st.require(conn, "search", search_id=body.search_id)
        st.require(conn, "arm", arm_id=body.arm_id)
        conn.execute("UPDATE search SET shipping_arm_id = ? WHERE search_id = ?", (body.arm_id, body.search_id))
    return OK
