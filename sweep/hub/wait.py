"""sweep/hub/wait.py -- the hub's own loop: nothing ever reads an agent's log.

An active run whose host's heartbeat has expired is posted `failed` with the count reached (a silent agent is a
different answer from zero); its queue entry stays, so a restarted agent claims it back and skips the cells already
recorded. An ack completes a run only when its records match the plan, stage by stage, and fails it otherwise. Stdlib.
"""
import threading
import time

from sweep.hub import store as st

TERMINAL = ("complete", "failed", "abandoned")
ENCODING_STAGES = frozenset({"screen", "locate", "encode", "time", "split", "concurrency", "viewing", "probe", "calibrate"})


def verify_at_ack(conn, run, body):
    """The reason the run is not complete, or None: what the plan promised and the record does not hold."""
    stage, rid = run["stage"], run["run_id"]
    if stage == "score":
        kept = [k for (k,) in conn.execute("SELECT c.cell_key FROM cell c JOIN encode e ON e.cell_key = c.cell_key AND e.kept = 1 "
                                            "WHERE c.run_id = ? ORDER BY c.cell_key", (run["parent_run_id"],))]
        missing = [k for k in kept if conn.execute("SELECT 1 FROM score WHERE run_id = ? AND cell_key = ?", (rid, k)).fetchone() is None]
        return None if not missing else f"{len(missing)} of {len(kept)} kept encodes have no score under this run"
    if stage in ("time", "split", "concurrency"):
        cells = body.get("cells", [])
        short, want = [], 1
        for c in cells:
            want = c.get("repeats", 1)
            (n,) = conn.execute("SELECT count(DISTINCT repeat_index) FROM timing WHERE cell_key = ?", (c["cell_key"],)).fetchone()
            if n < want:
                short.append(c["cell_key"])
        return None if not short else f"{len(short)} of {len(cells)} cells are short of their {want} repeats: {', '.join(short)}"
    if stage == "inventory":
        ids = [i["title_id"] for i in body.get("inputs", [])]
        missing = [t for t in ids if conn.execute("SELECT 1 FROM title WHERE title_id = ?", (t,)).fetchone() is None]
        return None if not missing else f"{len(missing)} of {len(ids)} titles have no record: {', '.join(missing)}"
    if stage == "materialise":
        ids = [c["cut_id"] for c in body.get("cuts", [])]
        missing = [c for c in ids if conn.execute("SELECT 1 FROM cut WHERE cut_id = ?", (c,)).fetchone() is None]
        return None if not missing else f"{len(missing)} of {len(ids)} cuts have no record: {', '.join(missing)}"
    total = conn.execute("SELECT count(*) FROM cell WHERE run_id = ?", (rid,)).fetchone()[0]
    (missing,) = conn.execute("SELECT count(*) FROM cell c WHERE c.run_id = ? AND NOT EXISTS (SELECT 1 FROM encode e WHERE e.cell_key = c.cell_key) "
                              "AND NOT EXISTS (SELECT 1 FROM cell_failure f WHERE f.cell_key = c.cell_key)", (rid,)).fetchone()
    return None if not missing else f"{missing} of {total} cells have no record"


def verify_publish(conn, job):
    """The reason a publish job is not done, or None: every file it named is a published row."""
    files = [f["relative"] for f in job.get("files", [])]
    missing = [f for f in files if conn.execute("SELECT 1 FROM published WHERE path = ?", (f,)).fetchone() is None]
    return None if not missing else f"{len(missing)} of {len(files)} files are not on the share: {', '.join(missing)}"


def sweep_once(store, queue):
    """Fail every active run whose host's heartbeat has expired; returns the run ids it failed."""
    with store.reading() as conn:
        active = conn.execute("SELECT run_id, host, stage FROM run WHERE state IN ('launched', 'running') ORDER BY run_id").fetchall()
    failed = []
    for run_id, host, stage in active:
        if queue.pulse(host) is not None:
            continue
        with store.transaction() as conn:
            total, still, scored = conn.execute("SELECT planned_total, still_planned, scored FROM v_run_progress WHERE run_id = ?", (run_id,)).fetchone()
            done = scored if stage == "score" else total - still
            detail = f"heartbeat expired; {done} of {total} cells done"
            st.post_event(conn, run_id, st.stamp(), "failed", detail)
        queue.publish(run_id, {"run_id": run_id, "state": "failed", "detail": detail, "by": "hub", "final": True})
        failed.append(run_id)
    return failed


def run_forever(store, queue, period_s=10.0, stop=None):
    """A daemon thread's target: sweep every period until `stop` (a threading.Event) is set; an error is logged, never fatal."""
    stop = stop or threading.Event()
    while not stop.is_set():
        try:
            sweep_once(store, queue)
        except Exception as e:      # noqa: BLE001 -- the loop outlives any one failure
            print(f"wait: {e}", flush=True)
        stop.wait(period_s)
