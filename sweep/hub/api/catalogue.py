"""sweep/hub/api/catalogue.py -- the catalogue verbs: one POST per FILE table group, each one checked transaction."""
import json

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


class AddLadder(Body):
    ladder_id: str
    codec: str
    rungs: list[int]


@router.post("/add-ladder")
def add_ladder(body: AddLadder, store=Depends(get_store)):
    """One ladder per codec, never per host; the rungs a shipped anchor may take."""
    with store.transaction() as conn:
        st.insert(conn, "ladder", {"ladder_id": body.ladder_id, "codec": body.codec})
        for rung in body.rungs:
            st.insert(conn, "ladder_rung", {"ladder_id": body.ladder_id, "rung": rung})
    return OK


class AuthorChain(Body):
    lane: str
    host: str
    encoder_unit_id: str
    vf_template: str
    notes_ref: str | None = None


@router.post("/author-chain")
def author_chain(body: AuthorChain, store=Depends(get_store)):
    """The production filter graph per (lane, host, unit), authored before the sample."""
    with store.transaction() as conn:
        st.require(conn, "lane", lane=body.lane)
        st.require(conn, "host", host=body.host)
        st.require(conn, "host_unit", host=body.host, encoder_unit_id=body.encoder_unit_id)
        st.insert(conn, "chain", body.model_dump())
    return OK


class AddScorer(Body):
    host: str
    ffvship: list[str]            # argv
    score_ffmpeg: list[str]       # argv
    metric_backend: str
    gpu_id: int
    cache_dir: str


@router.post("/add-scorer")
def add_scorer(body: AddScorer, store=Depends(get_store)):
    """The intent of scoring on a host; the argv are stored as compact JSON."""
    with store.transaction() as conn:
        st.require(conn, "host", host=body.host)
        st.insert(conn, "scorer", {"host": body.host, "ffvship": json.dumps(body.ffvship, separators=(",", ":")),
                                   "score_ffmpeg": json.dumps(body.score_ffmpeg, separators=(",", ":")),
                                   "metric_backend": body.metric_backend, "gpu_id": body.gpu_id, "cache_dir": body.cache_dir})
    return OK


class SetFloor(Body):
    lane: str
    min_content_rate: float | None = None


@router.post("/set-floor")
def set_floor(body: SetFloor, store=Depends(get_store)):
    """The throughput floor a host must reach for the lane; NULL reports only. The shipped rate must meet it, measured."""
    with store.transaction() as conn:
        st.require(conn, "lane", lane=body.lane)
        conn.execute("UPDATE lane SET min_content_rate = ? WHERE lane = ?", (body.min_content_rate, body.lane))
    return OK


class BlockHost(Body):
    host: str
    fix: str


@router.post("/block-host")
def block_host(body: BlockHost, store=Depends(get_store)):
    """A host is blocked with THE FIX; a run on it is refused at the moment of use, quoting it."""
    if not body.fix.strip():
        raise Refusal("block-host without the fix", "give --fix: what must be done before the host is usable; the refusal at the moment of use quotes it")
    with store.transaction() as conn:
        st.require(conn, "host", host=body.host)
        conn.execute("UPDATE host SET blocked = ? WHERE host = ?", (body.fix, body.host))
    return OK


class UnblockHost(Body):
    host: str


@router.post("/unblock-host")
def unblock_host(body: UnblockHost, store=Depends(get_store)):
    with store.transaction() as conn:
        st.require(conn, "host", host=body.host)
        conn.execute("UPDATE host SET blocked = NULL WHERE host = ?", (body.host,))
    return OK
