"""sweep/hub/api/runs.py -- the mechanical verbs: each a plan the hub builds from the store's intent and enqueues in one
checked transaction (inventory, materialise --adopt, encode, score, time, publish); abandon posts the event an agent reads
between cells; watch follows a run's events; status answers from the store and the heartbeats, so a silent agent is
reported silent, never as zero. The agent endpoints are sweep/hub/api/agents.py."""
import datetime as dt
import json

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse

from sweep.hub import artifact, planner, store as st
from sweep.hub.api.common import OK, Body, get_queue, get_store, stamp
from sweep.hub.refusals import Refusal

router = APIRouter(prefix="/runs", tags=["runs"])
TERMINAL = ("complete", "failed", "abandoned")


def _now():
    return dt.datetime.now(dt.timezone.utc)


def _planned(store, queue, plan_fn, *args, **kw):
    with store.reading() as conn:
        plan, body = plan_fn(conn, *args, **kw)
    entry_id = planner.enqueue(store, queue, plan, body)
    return {"run_id": plan.run_id, "entry_id": entry_id}


class TitleRef(Body):
    title_id: str
    path: str


class Inventory(Body):
    host: str
    library: str
    titles: list[TitleRef]


@router.post("/inventory")
def inventory(body: Inventory, store=Depends(get_store), queue=Depends(get_queue)):
    """A run on a host that mounts the library: the agent probes each named file and posts a title record."""
    return _planned(store, queue, planner.plan_inventory, body.host, body.library, [t.model_dump() for t in body.titles], _now())


class AdoptCut(Body):
    window_id: str
    kind: str                      # reference | source
    path: str                      # where the file is, in the host's spelling


class Materialise(Body):
    host: str
    encoder_unit_id: str           # a unit in this box; the run's identity
    reference_set_id: str
    geometry: str
    pix_fmt: str
    chain_lane: str                # the chain that BUILT the cuts, wherever that was
    chain_host: str
    chain_unit: str
    cuts: list[AdoptCut]
    adopt: bool = True


@router.post("/materialise")
def materialise(body: Materialise, store=Depends(get_store), queue=Depends(get_queue)):
    """Adopt existing cuts: the agent hashes, counts frames and copies each into the reference set's home under its work root."""
    if not body.adopt:
        raise Refusal("materialise without --adopt cuts the windows, and cutting is M4's", "pass --adopt with the existing cuts' paths")
    return _planned(store, queue, planner.plan_adopt, body.host, body.encoder_unit_id, body.reference_set_id, body.geometry, body.pix_fmt,
                    (body.chain_lane, body.chain_host, body.chain_unit), [c.model_dump() for c in body.cuts], _now())


class ViewingCell(Body):
    window_id: str
    settings: dict[str, str]


class Encode(Body):
    search_id: str | None = None
    content_class_id: str | None = None
    encoder_unit_id: str | None = None
    host: str
    stage: str = "encode"          # encode | viewing
    windows: list[str] = []
    rungs: list[int] = []
    arms: list[str] = []
    cells: list[ViewingCell] = []


@router.post("/encode")
def encode(body: Encode, store=Depends(get_store), queue=Depends(get_queue)):
    """Stage 5 from a search (the base arm on every in-range rung on every member, the candidates on their ladders, the
    incumbent at its anchor; or the named subset), or a viewing run of the cells the operator names."""
    viewing = bool(body.cells or body.content_class_id or body.encoder_unit_id)
    if body.search_id is not None and viewing:
        raise Refusal("encode takes a search with --windows, --rungs and --arms, or a viewing's class, unit and cells, not both", "drop one")
    if body.stage == "encode":
        if body.search_id is None:
            raise Refusal("encode needs --search-id", "the base arm and the ladder come from the search; a viewing names --stage viewing with its cells")
        return _planned(store, queue, planner.plan_encode, body.search_id, body.host, _now(),
                        windows=body.windows or None, rungs=body.rungs or None, arms=body.arms or None)
    if body.stage == "viewing":
        if not (body.content_class_id and body.encoder_unit_id and body.cells):
            raise Refusal("a viewing run needs --content-class-id, --encoder-unit-id and --cells", "name the class, the unit and each cell's window and settings")
        return _planned(store, queue, planner.plan_viewing, body.content_class_id, body.encoder_unit_id, body.host, [c.model_dump() for c in body.cells], _now())
    raise Refusal(f"stage {body.stage!r} is not planned by encode", "encode plans encode and viewing runs; screen, locate and the rest have their own verbs")


class Score(Body):
    run_id: str
    scorer: str | None = None
    keep: bool = False


@router.post("/score")
def score(body: Score, store=Depends(get_store), queue=Depends(get_queue)):
    """Stage 6: the run's kept encodes at the search's height (or the served lane's), on the scorer of its machine unless one is named."""
    return _planned(store, queue, planner.plan_score, body.run_id, _now(), scorer=body.scorer, keep=body.keep)


class Time(Body):
    run_id: str
    lane: str | None = None
    repeats: int = 5


@router.post("/time")
def time_(body: Time, store=Depends(get_store), queue=Depends(get_queue)):
    """Stage 7: every configuration the run encoded, on the source cut, through the lane's production chain, repeated."""
    return _planned(store, queue, planner.plan_time, body.run_id, _now(), lane=body.lane, repeats=body.repeats)


class Publish(Body):
    run_id: str | None = None
    cut_ids: list[str] = []
    via: str | None = None


@router.post("/publish")
def publish(body: Publish, store=Depends(get_store), queue=Depends(get_queue)):
    """A publish job on a host's queue: a run's kept encodes or cuts copied to the share, sha both ends."""
    with store.reading() as conn:
        host, job = planner.plan_publish(conn, run_id=body.run_id, cut_ids=body.cut_ids, via=body.via)
    return {"entry_id": queue.enqueue(host, None, job, kind="publish")}


class Abandon(Body):
    reason: str


@router.post("/{run_id}/abandon")
def abandon(run_id: str, body: Abandon, store=Depends(get_store), queue=Depends(get_queue)):
    """The operator's abandon: the event the agent reads between cells; a finished run is not abandoned."""
    with store.transaction() as conn:
        st.require(conn, "run", run_id=run_id)
        (state,) = conn.execute("SELECT state FROM run WHERE run_id = ?", (run_id,)).fetchone()
        if state in TERMINAL:
            raise Refusal(f"run {run_id} is {state}", "a finished run is not abandoned")
        st.post_event(conn, run_id, stamp(), "abandoned", body.reason)
    queue.publish(run_id, {"run_id": run_id, "state": "abandoned", "detail": body.reason, "by": "hub", "final": True})
    return OK


@router.get("/{run_id}/watch")
def watch(run_id: str, timeout: float = Query(600.0), store=Depends(get_store), queue=Depends(get_queue)):
    """The run's events so far, then whatever follows on its channel until a terminal one: server-sent events, never a log."""
    with store.reading() as conn:
        st.require(conn, "run", run_id=run_id)
        events = [{"run_id": run_id, "at": at, "state": state, "detail": detail, "by": by}
                  for at, state, detail, by in conn.execute("SELECT at, state, detail, by FROM run_event WHERE run_id = ? ORDER BY at", (run_id,))]

    def lines():
        last = None
        for e in events:
            last = e["state"]
            yield "data: " + json.dumps(dict(e, final=last in TERMINAL), sort_keys=True) + "\n\n"
        if last not in TERMINAL:
            for payload in queue.listen(run_id, timeout):
                yield "data: " + json.dumps(payload, sort_keys=True) + "\n\n"
    return StreamingResponse(lines(), media_type="text/event-stream")


@router.get("/status")
def status(store=Depends(get_store), queue=Depends(get_queue)):
    """Every run's progress from v_run_progress (the plan is its cells), every host with its block, heartbeat and identity, the queue per host."""
    with store.reading() as conn:
        cols = ["run_id", "host", "stage", "state", "planned_total", "still_planned", "encoded", "scored", "timed", "failed"]
        runs = [dict(zip(cols, r)) for r in conn.execute(
            "SELECT p.run_id, r.host, p.stage, p.state, p.planned_total, p.still_planned, p.encoded, p.scored, p.timed, p.failed "
            "FROM v_run_progress p JOIN run r ON r.run_id = p.run_id ORDER BY r.started_at, p.run_id")]
        hosts = [{"host": h, "blocked": b} for h, b in conn.execute("SELECT host, blocked FROM host ORDER BY host")]
        identities = {h["host"]: (i.artifact if (i := artifact.current_identity(conn, h["host"])) else None) for h in hosts}
    return {"runs": runs, "hosts": hosts, "queue": {h["host"]: len(queue.pending(h["host"])) for h in hosts},
            "heartbeats": {h["host"]: queue.pulse(h["host"]) for h in hosts}, "identities": identities}
