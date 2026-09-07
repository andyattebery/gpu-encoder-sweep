"""sweep/hub/api/runs.py -- the runs' state as the hub sees it. The agent endpoints (claim, heartbeat, events, records) are M2's."""
from fastapi import APIRouter, Depends

from sweep.hub.api.common import get_queue, get_store

router = APIRouter(prefix="/runs", tags=["runs"])


@router.get("/status")
def status(store=Depends(get_store), queue=Depends(get_queue)):
    """Every run's progress from v_run_progress (the plan is its cells), every host and whether it is blocked, the queue per host."""
    with store.reading() as conn:
        cols = ["run_id", "host", "stage", "state", "planned_total", "still_planned", "encoded", "scored", "timed", "failed"]
        runs = [dict(zip(cols, r)) for r in conn.execute(
            "SELECT p.run_id, r.host, p.stage, p.state, p.planned_total, p.still_planned, p.encoded, p.scored, p.timed, p.failed "
            "FROM v_run_progress p JOIN run r ON r.run_id = p.run_id ORDER BY r.started_at, p.run_id")]
        hosts = [{"host": h, "blocked": b} for h, b in conn.execute("SELECT host, blocked FROM host ORDER BY host")]
    return {"runs": runs, "hosts": hosts, "queue": {h["host"]: len(queue.pending(h["host"])) for h in hosts}}
