"""sweep/hub/api/agents.py -- what an agent speaks to the hub, and nothing else speaks: its config, its heartbeat, the
claim that hands it a run for the artifact it reports, the events and records it posts as it goes, the per-frame values
it uploads, the ack the hub completes or fails a run at, the abandon flag it reads between cells, and the exchange's
proof that a file reached the share. Every write is one checked transaction; every event is stamped by the hub."""
import gzip
import json
import os
import pathlib

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import PlainTextResponse

from sweep.hub import artifact, exchange, ingest, planner, store as st, wait
from sweep.hub.api.common import OK, Body, get_queue, get_store, stamp
from sweep.hub.refusals import Denied, Refusal

router = APIRouter(tags=["agents"])
HEARTBEAT_S, TTL_S = 30, 90


def acting_for(request, host):
    """The token presented is this host's, or the hub is open (no token configured)."""
    actor = getattr(request.state, "agent_host", None)
    if actor is not None and actor != host:
        raise Denied(f"the token presented is {actor}'s, not {host}'s", "an agent acts for its own host only")


def _run(conn, run_id):
    st.require(conn, "run", run_id=run_id)
    row = conn.execute("SELECT run_id, stage, parent_run_id, search_id, content_class_id, host, state, artifact FROM run WHERE run_id = ?", (run_id,)).fetchone()
    return dict(zip(("run_id", "stage", "parent_run_id", "search_id", "content_class_id", "host", "state", "artifact"), row))


@router.get("/agents/{host}/config")
def config(host: str, request: Request, store=Depends(get_store)):
    """The host row and its scorer row: everything an agent needs beyond its three environment variables."""
    acting_for(request, host)
    with store.reading() as conn:
        st.require(conn, "host", host=host)
        cur = conn.execute("SELECT * FROM host WHERE host = ?", (host,))
        row = dict(zip([d[0] for d in cur.description], cur.fetchone()))
        scorer = planner._scorer(conn, host)
    return {"host": row, "scorer": scorer, "heartbeat_s": HEARTBEAT_S, "ttl_s": TTL_S}


class IdentityBody(Body):
    artifact: str
    harness_version: str
    ffmpeg_build: str
    ffmpeg_sha: str
    ffmpeg_filters: list[str]
    ffvship_version: str | None = None
    free_bytes: int


class Heartbeat(Body):
    run_id: str | None = None
    cells_done: int = 0
    cells_total: int = 0
    artifact: str
    identity: IdentityBody


@router.post("/agents/{host}/heartbeat")
def heartbeat(host: str, body: Heartbeat, request: Request, store=Depends(get_store), queue=Depends(get_queue)):
    """Kept with a TTL; an identity that differs from the host's current one is recorded, so a plan pins what runs."""
    acting_for(request, host)
    identity = artifact.Identity(**dict(body.identity.model_dump(), ffmpeg_filters=tuple(body.identity.ffmpeg_filters)))
    with store.reading() as conn:
        st.require(conn, "host", host=host)
        current = artifact.current_identity(conn, host)
        changed = current is None or current._replace(free_bytes=0) != identity._replace(free_bytes=0)
        state = None
        if body.run_id is not None:
            row = conn.execute("SELECT state FROM run WHERE run_id = ?", (body.run_id,)).fetchone()
            state = None if row is None else row[0]
    if changed:
        with store.transaction() as conn:
            artifact.record_identity(conn, host, identity, stamp())
    queue.beat(host, {"run_id": body.run_id, "cells_done": body.cells_done, "cells_total": body.cells_total, "artifact": body.artifact, "at": stamp()}, TTL_S)
    return {"ok": True, "run_state": state}


class Claim(Body):
    block_s: int = 30


@router.post("/agents/{host}/claim")
def claim(host: str, body: Claim, request: Request, store=Depends(get_store), queue=Depends(get_queue)):
    """The host's own unacked entry first, else the next queued: a run is launched here, for the artifact the agent reports,
    once its machine is quiet if it is a timing run; a stale entry is acked away; nothing waiting is 204."""
    acting_for(request, host)
    with store.reading() as conn:
        st.require(conn, "host", host=host)
    entry = queue.claim(host, body.block_s)
    if entry is None:
        return Response(status_code=204)
    if entry.kind == "publish":
        with store.reading() as conn:
            return {**planner.claim_body(conn, entry), "entry_id": entry.entry_id}
    with store.reading() as conn:
        row = conn.execute("SELECT state, artifact, stage, host FROM run WHERE run_id = ?", (entry.run_id,)).fetchone()
        current = artifact.current_identity(conn, host)
    if row is None or row[0] in ("complete", "abandoned") or row[3] != host:
        queue.ack(host, entry.entry_id)
        queue.publish(entry.run_id, {"run_id": entry.run_id, "dropped": "a stale entry: the run is finished, or not this host's", "by": "hub"})
        return Response(status_code=204)
    state, planned_for, stage, _ = row
    if current is None or current.artifact != planned_for:
        reported = current.artifact if current is not None else "no identity"
        return PlainTextResponse(str(Refusal(f"run {entry.run_id} was planned for artifact {planned_for}; {host} reports {reported}",
                                             "update the node to the plan's artifact, or abandon and re-plan")), status_code=409)
    with store.reading() as conn:
        if stage in planner.QUIET_STAGES and planner.busy_on_machine(conn, host, entry.run_id) is not None:
            return Response(status_code=204)                       # the entry stays this host's; it is handed out once the machine is quiet
    with store.transaction() as conn:
        if state in ("planned", "failed"):                        # a restarted agent taking its launched or running run back posts nothing
            st.post_event(conn, entry.run_id, stamp(), "launched", f"claimed by {host}; artifact {planned_for}")
            queue.publish(entry.run_id, {"run_id": entry.run_id, "state": "launched", "detail": f"claimed by {host}", "by": "hub"})
        claimed = planner.claim_body(conn, entry)
    return {**claimed, "kind": "run", "entry_id": entry.entry_id}


class Event(Body):
    state: str
    detail: str | None = None


@router.post("/runs/{run_id}/events")
def events(run_id: str, body: Event, request: Request, store=Depends(get_store), queue=Depends(get_queue)):
    """running when the first cell starts, failed when the agent gives up; the hub stamps it and completes a run at ack."""
    if body.state not in ("running", "failed"):
        raise Refusal("an agent may post running or failed", "the hub completes a run at ack")
    with store.transaction() as conn:
        run = _run(conn, run_id)
        acting_for(request, run["host"])
        st.post_event(conn, run_id, stamp(), body.state, body.detail, by="agent")
    queue.publish(run_id, {"run_id": run_id, "state": body.state, "detail": body.detail, "by": "agent", "final": body.state == "failed"})
    return OK


class Records(Body):
    records: list[ingest.Record]


@router.post("/runs/{run_id}/records")
def records(run_id: str, body: Records, request: Request, store=Depends(get_store), queue=Depends(get_queue)):
    """One transaction for the batch: every record ingested and every check run, or none of it."""
    with store.transaction() as conn:
        run = _run(conn, run_id)
        acting_for(request, run["host"])
        for record in body.records:
            ingest.ingest_record(conn, run_id, record)
    for record in body.records:
        queue.publish(run_id, {"run_id": run_id, "record": record.kind, "cell_key": getattr(record, "cell_key", None), "by": "agent"})
    return {"ok": True, "ingested": len(body.records)}


class Frames(Body):
    cell_key: str
    metric: str
    values: list[float]


@router.post("/runs/{run_id}/frames")
def frames(run_id: str, body: Frames, request: Request, store=Depends(get_store)):
    """Per-frame values, kept gzipped beside the store and outside the export; the height is the run's, never a field."""
    root = request.app.state.frames
    if root is None:
        raise Refusal("the hub keeps no per-frame values: SWEEP_FRAMES is unset", "set SWEEP_FRAMES to a directory beside the store")
    with store.reading() as conn:
        run = _run(conn, run_id)
        acting_for(request, run["host"])
        height = ingest.score_height(conn, run)
    path = pathlib.Path(root) / run_id / f"{body.cell_key}.{height}.{body.metric}.json.gz"
    data = json.dumps(body.values, separators=(",", ":")).encode()
    if path.exists():
        if gzip.open(path).read() == data:
            return OK
        raise Refusal(f"{run_id} already holds frames for {body.cell_key} {body.metric} that differ", ingest.NEVER_OVERWRITTEN)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with gzip.GzipFile(tmp, "wb", mtime=0) as f:
        f.write(data)
    os.replace(tmp, path)
    return OK


class Ack(Body):
    entry_id: str


@router.post("/runs/{run_id}/ack")
def ack(run_id: str, body: Ack, request: Request, store=Depends(get_store), queue=Depends(get_queue)):
    """The hub verifies the records against the plan: complete with verified_at, or failed with what is missing; then the entry goes."""
    with store.transaction() as conn:
        run = _run(conn, run_id)
        acting_for(request, run["host"])
        entry = next((e for e in queue.pending(run["host"]) if e.entry_id == body.entry_id), None)
        if entry is None:
            raise Refusal(f"entry {body.entry_id!r} is not claimed by {run['host']}", "claim before ack")
        if run["state"] == "abandoned":                        # the agent stopped between cells as asked: the entry goes, the state stands
            state = "abandoned"
        else:
            reason = wait.verify_at_ack(conn, run, entry.plan)
            state = "complete" if reason is None else "failed"
            st.post_event(conn, run_id, stamp(), state, "verified against the plan: every cell recorded" if reason is None else f"acked with {reason}")
    queue.ack(run["host"], body.entry_id)
    queue.publish(run_id, {"run_id": run_id, "state": state, "by": "hub", "final": True})
    return {"ok": True, "state": state}


@router.post("/agents/{host}/ack")
def job_ack(host: str, body: Ack, request: Request, store=Depends(get_store), queue=Depends(get_queue)):
    """A publish job's ack: every file it named is a published row, or the ack is refused and the job stays."""
    acting_for(request, host)
    entry = next((e for e in queue.pending(host) if e.entry_id == body.entry_id and e.kind == "publish"), None)
    if entry is None:
        raise Refusal(f"entry {body.entry_id!r} is not a publish job claimed by {host}", "claim before ack; a run is acked at /runs/{id}/ack")
    with store.reading() as conn:
        reason = wait.verify_publish(conn, entry.plan)
    if reason is not None:
        raise Refusal(reason, "publish them before the ack")
    queue.ack(host, body.entry_id)
    return OK


@router.get("/runs/{run_id}/abandon")
def abandon_flag(run_id: str, request: Request, store=Depends(get_store)):
    """Read between cells: whether the operator abandoned the run, and why."""
    with store.reading() as conn:
        run = _run(conn, run_id)
        acting_for(request, run["host"])
        row = conn.execute("SELECT detail FROM run_event WHERE run_id = ? AND state = 'abandoned' ORDER BY at DESC LIMIT 1", (run_id,)).fetchone()
    return {"abandon": run["state"] == "abandoned", "reason": None if row is None else row[0]}


class Published(Body):
    path: str
    by_host: str
    bytes: int
    sha256: str
    run_id: str | None = None
    cell_key: str | None = None
    cut_id: str | None = None


@router.post("/exchange/published")
def published(body: Published, request: Request, store=Depends(get_store)):
    """The agent's sha before the copy against the hub's after it, at the pool path; only an agreement is recorded."""
    acting_for(request, body.by_host)
    share = request.app.state.share
    if share is None:
        raise Refusal("the hub has no view of the share: SWEEP_SHARE is unset", "bind-mount temp/harness into the hub and set SWEEP_SHARE")
    exchange.verify_arrival(share, body.path, body.bytes, body.sha256)
    with store.transaction() as conn:
        st.require(conn, "host", host=body.by_host)
        exchange.record_publish(conn, body.path, body.by_host, body.bytes, body.sha256, stamp(), run_id=body.run_id, cell_key=body.cell_key, cut_id=body.cut_id)
    return OK
