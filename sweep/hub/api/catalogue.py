"""sweep/hub/api/catalogue.py -- the catalogue verbs: one POST per FILE table group, each one checked transaction."""
from fastapi import APIRouter, Depends

from sweep.hub import store as st
from sweep.hub.api.common import OK, Body, get_store
from sweep.hub.refusals import Refusal

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


class AddUnit(Body):
    encoder_unit_id: str
    vendor: str
    card: str
    driver: str
    frontend: str
    codec: str
    host: str
    device: str                   # by PCI path or a stable id; the DDL refuses a render node


@router.post("/add-unit")
def add_unit(body: AddUnit, store=Depends(get_store)):
    """The unit (once, by its identity) and its place in a host; an existing id must carry the same identity."""
    identity = (body.vendor, body.card, body.driver, body.frontend, body.codec)
    with store.transaction() as conn:
        st.require(conn, "host", host=body.host)
        row = conn.execute("SELECT vendor, card, driver, frontend, codec FROM encoder_unit WHERE encoder_unit_id = ?",
                           (body.encoder_unit_id,)).fetchone()
        if row is None:
            st.insert(conn, "encoder_unit", {"encoder_unit_id": body.encoder_unit_id, "vendor": body.vendor, "card": body.card,
                                             "driver": body.driver, "frontend": body.frontend, "codec": body.codec})
        elif tuple(row) != identity:
            raise Refusal(f"encoder_unit {body.encoder_unit_id!r} exists as {tuple(row)}",
                          "a unit is one (vendor, card, driver, frontend, codec); add-unit with the existing id puts it on another host, "
                          "and a different card or driver is a new id")
        st.insert(conn, "host_unit", {"host": body.host, "encoder_unit_id": body.encoder_unit_id, "device": body.device})
    return OK


class AddConcept(Body):
    canonical_id: str
    description: str


@router.post("/add-concept")
def add_concept(body: AddConcept, store=Depends(get_store)):
    with store.transaction() as conn:
        st.insert(conn, "canonical_concept", body.model_dump())
    return OK


class SettingScope(Body):
    encoder_unit_id: str
    applies: bool
    default_value: str | None = None
    default_is_measured: bool


class AddSetting(Body):
    setting_id: str
    flag: str
    frontend: str | None = None
    kind: str
    subsystem: str
    value_type: str
    range_lo: float | None = None
    range_hi: float | None = None
    is_generic: bool
    notes: str | None = None
    enum_values: list[str] = []
    roles: list[str] = []
    scope: list[SettingScope] = []


@router.post("/add-setting")
def add_setting(body: AddSetting, store=Depends(get_store)):
    """The setting, its enumeration, the concepts it carries and its scope per unit, in one transaction."""
    with store.transaction() as conn:
        st.insert(conn, "setting", {"setting_id": body.setting_id, "flag": body.flag, "frontend": body.frontend, "kind": body.kind,
                                    "subsystem": body.subsystem, "value_type": body.value_type, "range_lo": body.range_lo,
                                    "range_hi": body.range_hi, "is_generic": int(body.is_generic), "notes": body.notes})
        for value in body.enum_values:
            st.insert(conn, "setting_enum_value", {"setting_id": body.setting_id, "value": value})
        for concept in body.roles:
            st.require(conn, "canonical_concept", canonical_id=concept)
            st.insert(conn, "setting_role", {"setting_id": body.setting_id, "canonical_id": concept})
        for sc in body.scope:
            st.require(conn, "encoder_unit", encoder_unit_id=sc.encoder_unit_id)
            st.insert(conn, "setting_scope", {"setting_id": body.setting_id, "encoder_unit_id": sc.encoder_unit_id, "applies": int(sc.applies),
                                              "default_value": sc.default_value, "default_is_measured": int(sc.default_is_measured)})
    return OK


class AddLane(Body):
    lane: str
    codec: str
    decision_rule: str
    input_width_min: int | None = None
    input_width_max: int | None = None
    input_dynamic_range: str
    output_resolution: str
    output_dynamic_range: str
    hdr_handling: str
    audio: str
    subtitles: str
    score_target: float | None = None
    score_height: int
    bitrate_cap_binds: str
    bitrate_cap_constant: str | None = None
    has_content: bool
    min_content_rate: float | None = None
    steps: list[str]


@router.post("/add-lane")
def add_lane(body: AddLane, store=Depends(get_store)):
    with store.transaction() as conn:
        if body.bitrate_cap_constant is not None:
            st.require(conn, "constant", name=body.bitrate_cap_constant)
        row = body.model_dump(exclude={"steps"})
        row["has_content"] = int(body.has_content)
        st.insert(conn, "lane", row)
        for step in body.steps:
            st.insert(conn, "lane_step", {"lane": body.lane, "step": step})
    return OK


class AddConstant(Body):
    name: str
    unit: str
    provenance: str
    value: float | None = None
    inputs_json: str | None = None
    precision: str | None = None
    reason: str | None = None
    cites_json: str | None = None


@router.post("/add-constant")
def add_constant(body: AddConstant, store=Depends(get_store)):
    with store.transaction() as conn:
        st.insert(conn, "constant", body.model_dump())
    return OK


class ScopeConstant(Body):
    name: str
    lanes: list[str]


@router.post("/scope-constant")
def scope_constant(body: ScopeConstant, store=Depends(get_store)):
    """The lanes a constant admits; a measured constant scoped to a shipped lane must have been calibrated."""
    with store.transaction() as conn:
        st.require(conn, "constant", name=body.name)
        for lane in body.lanes:
            st.require(conn, "lane", lane=lane)
            st.insert(conn, "constant_scope", {"name": body.name, "lane": lane})
    return OK
