"""sweep/hub/planner.py -- the stage planners: the store's intent becomes run rows and the body an agent is handed.

Every plan is rows before it runs (the run, its windows, every cell with its settings), with the cell keys computed
by K1, every argv composed by the one builder, and the artifact pinned from the identity the host's agent last
reported. `enqueue` writes the rows in one checked transaction and the queue entry after it; the claim adds the cells
already recorded, so a restarted agent skips them. A publish job is a queue entry of its own kind, not a run.
"""
import dataclasses
import datetime as dt
import json

from sweep import recipes
from sweep.hub import artifact, build, exchange, ingest, store as st
from sweep.hub.refusals import Refusal
from sweep.hub.store import CellPlan, RunPlan

RECIPES = {"score": "S1", "key": "K1"}
KEPT_STAGES = frozenset({"encode", "locate", "viewing"})      # an encode that is scored later, or kept for eyes; the rest discard
QUIET_STAGES = frozenset({"time", "split", "concurrency"})


def run_id(stage, subject, now):
    return f"{stage}-{subject}-{now.astimezone(dt.timezone.utc):%Y%m%dT%H%M%SZ}"


_new_run_id = run_id      # the planners take run_id as a parameter and stamp new ids through this alias


# ---------------------------------------------------------------- the store's rows, by name

def _row(conn, table, **key):
    st.require(conn, table, **key)
    where = " AND ".join(f"{k} = ?" for k in key)
    cur = conn.execute(f"SELECT * FROM {table} WHERE {where}", tuple(key.values()))
    return dict(zip([d[0] for d in cur.description], cur.fetchone()))


def _identity(conn, host):
    identity = artifact.current_identity(conn, host)
    if identity is None:
        raise Refusal(f"{host} has never reported an identity", "start its agent; a plan is built for the code the node runs")
    return identity


def _unit_on_host(conn, host, encoder_unit_id):
    """(frontend, codec, device): the unit as it sits in this box."""
    unit = _row(conn, "encoder_unit", encoder_unit_id=encoder_unit_id)
    hu = _row(conn, "host_unit", host=host, encoder_unit_id=encoder_unit_id)
    return unit["frontend"], unit["codec"], hu["device"]


def _scorer(conn, host):
    row = conn.execute("SELECT ffvship, score_ffmpeg, metric_backend, gpu_id, cache_dir FROM scorer WHERE host = ?", (host,)).fetchone()
    if row is None:
        return None
    return {"ffvship": json.loads(row[0]), "score_ffmpeg": json.loads(row[1]), "metric_backend": row[2], "gpu_id": row[3], "cache_dir": row[4]}


def _cut(conn, reference_set_id, window_id, kind):
    row = conn.execute("SELECT cut_id, content_sha, frames FROM cut WHERE reference_set_id = ? AND window_id = ? AND kind = ?",
                       (reference_set_id, window_id, kind)).fetchone()
    if row is None:
        raise Refusal(f"no {kind} cut of window {window_id!r} in reference set {reference_set_id!r}", "materialise the window first")
    return {"cut_id": row[0], "window_id": window_id, "kind": kind, "content_sha": row[1], "frames": row[2]}


def _members(conn, content_class_id):
    return [w for (w,) in conn.execute("SELECT window_id FROM content_class_member WHERE content_class_id = ? ORDER BY window_id", (content_class_id,))]


def _served_lanes(conn, content_class_id):
    return [l for (l,) in conn.execute("SELECT lane FROM content_class_lane WHERE content_class_id = ? ORDER BY lane", (content_class_id,))]


def _refset_holder(conn, machine, window_id):
    """A host on the machine whose complete materialise run covered the window: its work root holds the reference set."""
    row = conn.execute("SELECT r.host FROM run r JOIN host h ON h.host = r.host JOIN run_window rw ON rw.run_id = r.run_id "
                       "WHERE r.stage = 'materialise' AND r.state = 'complete' AND h.machine = ? AND rw.window_id = ? "
                       "ORDER BY r.finished_at DESC LIMIT 1", (machine, window_id)).fetchone()
    return None if row is None else row[0]


def _published(conn, relative):
    row = conn.execute("SELECT sha256, bytes FROM published WHERE path = ?", (relative,)).fetchone()
    return None if row is None else {"relative": relative, "sha256": row[0], "bytes": row[1]}


def node_label(host, encoder_unit_id):
    return host if encoder_unit_id is None else f"{host}:{encoder_unit_id}"


def default_resolved(conn, encoder_unit_id, identity):
    """The unit's scoped defaults the identity does not set, recorded so "absent" never needs interpreting later."""
    return [(s, v, "default_resolved") for s, v in conn.execute(
        "SELECT setting_id, default_value FROM setting_scope WHERE encoder_unit_id = ? AND applies = 1 AND default_value IS NOT NULL "
        "ORDER BY setting_id", (encoder_unit_id,)) if s not in identity]


def _run_section(plan, host_row, identity, device=None, tools=None, **extra):
    return {"run_id": plan.run_id, "stage": plan.stage, "host": plan.host, "node_label": plan.node_label,
            "encoder_unit_id": plan.encoder_unit_id, "content_class_id": plan.content_class_id, "search_id": plan.search_id,
            "device": device, "tools": tools or {"ffmpeg": host_row["ffmpeg"]}, "work_root": host_row["work_root"],
            "share_root": host_row["share_root"], "versions": artifact.pins(identity), "recipes": dict(RECIPES), "artifact": identity.artifact, **extra}


def _body(run, windows=(), inputs=(), cells=(), score=None, exchange_=(), **extra):
    return {"run": run, "windows": list(windows), "inputs": list(inputs), "cells": list(cells), "score": score, "exchange": list(exchange_),
            "done": [], **extra}


# ---------------------------------------------------------------- inventory and adopt

def plan_inventory(conn, host, library, titles, now):
    host_row = _row(conn, "host", host=host)
    identity = _identity(conn, host)
    plan = RunPlan(run_id=_new_run_id("inventory", host, now), stage="inventory", host=host, node_label=host, planned_at=now.isoformat(), **artifact.pins(identity))
    inputs = [{"title_id": t["title_id"], "path": t["path"], "library": library} for t in titles]
    return plan, _body(_run_section(plan, host_row, identity), inputs=inputs)


def plan_adopt(conn, host, encoder_unit_id, reference_set_id, geometry, pix_fmt, chain, cuts, now):
    """Register existing cut files on this host: the agent hashes, counts frames and copies each into the reference set's
    home under its work root. The run's unit is one in this box; the cuts name the chain that built them, wherever that was."""
    host_row = _row(conn, "host", host=host)
    identity = _identity(conn, host)
    _unit_on_host(conn, host, encoder_unit_id)
    lane, chain_host, chain_unit = chain
    st.require(conn, "chain", lane=lane, host=chain_host, encoder_unit_id=chain_unit)
    windows = sorted({c["window_id"] for c in cuts})
    for w in windows:
        st.require(conn, "window", window_id=w)
    exists = conn.execute("SELECT 1 FROM reference_set WHERE reference_set_id = ?", (reference_set_id,)).fetchone() is not None
    plan = RunPlan(run_id=_new_run_id("materialise", reference_set_id, now), stage="materialise", host=host, node_label=node_label(host, encoder_unit_id),
                   encoder_unit_id=encoder_unit_id, windows=tuple(windows), planned_at=now.isoformat(), **artifact.pins(identity))
    out = []
    for c in cuts:
        kind = c["kind"]
        relative = exchange.share_path("cut", reference_set_id=reference_set_id, window_id=c["window_id"], cut_kind=kind)
        out.append({"cut_id": f"{c['window_id']}.{'ref' if kind == 'reference' else 'src'}", "window_id": c["window_id"], "kind": kind,
                    "path": c["path"], "dest": exchange.work_path(host_row, relative),
                    "chain": {"lane": lane, "host": chain_host, "encoder_unit_id": chain_unit} if kind == "reference" else None})
    body = _body(_run_section(plan, host_row, identity), windows=windows,
                 reference_set={"id": reference_set_id, "geometry": geometry, "pix_fmt": pix_fmt, "post": not exists}, cuts=out)
    return plan, body


# ---------------------------------------------------------------- the encoding stages

def _encode_cells(conn, host_row, identity, encoder_unit_id, reference_set_id, rid, stage, wanted, repeats=1, chain=None):
    """wanted: [(window_id, cut_kind, {setting_id: value} identity)] -> (CellPlans, body cells, inputs, skipped keys)."""
    frontend, codec, device = _unit_on_host(conn, host_row["host"], encoder_unit_id)
    keep = stage in KEPT_STAGES
    cells, specs, inputs, skipped, seen = [], [], {}, [], set()
    for window_id, cut_kind, ident in wanted:
        for setting_id in ident:
            st.require(conn, "setting", setting_id=setting_id)
        cut = _cut(conn, reference_set_id, window_id, cut_kind)
        pairs = sorted(ident.items())
        key = recipes.cell_key(encoder_unit_id, cut["content_sha"], cut_kind, window_id, pairs, identity.ffmpeg_build)
        if key in seen:
            continue                     # the same configuration on the same cut is one cell (the incumbent on a base rung)
        seen.add(key)
        if conn.execute("SELECT 1 FROM cell WHERE cell_key = ?", (key,)).fetchone():
            skipped.append(key)
            continue
        settings = [(s, v, "identity") for s, v in pairs] + default_resolved(conn, encoder_unit_id, ident)
        relative = exchange.share_path("cut", reference_set_id=reference_set_id, window_id=window_id, cut_kind=cut_kind)
        src = exchange.work_path(host_row, relative)
        dst = exchange.work_path(host_row, exchange.share_path("enc", run_id=rid, cell_key=key))
        argv = build.encode_argv(frontend, codec, build.ordered_flags(conn, settings), device, src, dst,
                                 vf_template=None if chain is None else chain["vf_template"])
        cells.append(CellPlan(key, window_id, cut_kind, tuple(settings)))
        specs.append({"cell_key": key, "window_id": window_id, "cut_kind": cut_kind, "settings": [list(s) for s in settings], "argv": argv,
                      "output": dst, "keep": keep, "repeats": repeats, "workers": 1, "legs": ["full"]})
        inputs.setdefault((window_id, cut_kind), dict(cut, path=src, probe_argv=None if cut_kind == "reference" else build.probe_argv(frontend, device, src)))
    return cells, specs, [inputs[k] for k in sorted(inputs)], skipped, device


def _with_cells(plan, cells):
    return dataclasses.replace(plan, cells=tuple(cells))


def plan_viewing(conn, content_class_id, encoder_unit_id, host, cells, now):
    """A viewing run: the operator names each cell's window and identity settings; the encodes are kept for eyes."""
    host_row = _row(conn, "host", host=host)
    identity = _identity(conn, host)
    cc = _row(conn, "content_class", content_class_id=content_class_id)
    members = _members(conn, content_class_id)
    wanted = []
    for c in cells:
        if c["window_id"] not in members:
            raise Refusal(f"window {c['window_id']!r} is not a member of {content_class_id}", "view a window of the class; define-class adds members")
        wanted.append((c["window_id"], "reference", {k: str(v) for k, v in c["settings"].items()}))
    windows = tuple(sorted({w for w, _, _ in wanted}))
    rid = _new_run_id("viewing", content_class_id, now)
    plan = RunPlan(run_id=rid, stage="viewing", host=host, node_label=node_label(host, encoder_unit_id), encoder_unit_id=encoder_unit_id,
                   content_class_id=content_class_id, windows=windows, planned_at=now.isoformat(), **artifact.pins(identity))
    planned, specs, inputs, skipped, device = _encode_cells(conn, host_row, identity, encoder_unit_id, cc["reference_set_id"], rid, "viewing", wanted)
    if not planned:
        raise Refusal(f"nothing to encode: every cell of the viewing on {host} exists already", "a cell is planned once; view other settings or windows")
    plan = _with_cells(plan, planned)
    return plan, _body(_run_section(plan, host_row, identity, device=device), windows=windows, inputs=inputs, cells=specs, skipped=skipped)


def plan_encode(conn, search_id, host, now, windows=None, rungs=None, arms=None):
    """Stage 5: the base arm on every rung of the codec ladder inside the anchor's range on every member; each candidate on its
    derived ladder; the incumbent at its pinned anchor -- or the named subset of windows, rungs and arms."""
    search = _row(conn, "search", search_id=search_id)
    host_row = _row(conn, "host", host=host)
    identity = _identity(conn, host)
    unit_id = search["encoder_unit_id"]
    codec = _row(conn, "encoder_unit", encoder_unit_id=unit_id)["codec"]
    cc = _row(conn, "content_class", content_class_id=search["content_class_id"])
    anchor = _row(conn, "setting", setting_id=search["anchor_setting_id"])
    members = _members(conn, search["content_class_id"])
    for w in windows or ():
        if w not in members:
            raise Refusal(f"window {w!r} is not a member of {search['content_class_id']}", "encode the class's members; define-class adds one")
    windows = list(windows) if windows else members
    ladder = [r for (r,) in conn.execute("SELECT r.rung FROM ladder_rung r JOIN ladder l ON l.ladder_id = r.ladder_id WHERE l.codec = ? ORDER BY r.rung", (codec,))]
    lo, hi = anchor["range_lo"], anchor["range_hi"]
    in_range = [r for r in ladder if (lo is None or r >= lo) and (hi is None or r <= hi)]
    for r in rungs or ():
        if r not in in_range:
            raise Refusal(f"rung {r} is not on the {codec} ladder inside {anchor['setting_id']}'s range {lo}..{hi}",
                          "encode the ladder's in-range rungs; a rung past the range is UNREACHABLE, never a cell")
    rungs = list(rungs) if rungs else in_range
    arm_rows = {a: (role, value) for a, role, value in conn.execute("SELECT arm_id, role, anchor_value FROM arm WHERE search_id = ? ORDER BY arm_id", (search_id,))}
    for a in arms or ():
        if a not in arm_rows:
            raise Refusal(f"{a!r} is not an arm of search {search_id}", "encode the search's own arms; author-search names them")
    chosen = list(arms) if arms else list(arm_rows)
    wanted = []
    for arm_id in chosen:
        role, anchor_value = arm_rows[arm_id]
        settings = dict(conn.execute("SELECT setting_id, value FROM arm_setting WHERE arm_id = ?", (arm_id,)).fetchall())
        if role == "base":
            wanted += [(w, "reference", {**settings, anchor["setting_id"]: str(r)}) for w in windows for r in rungs]
        elif role == "incumbent":
            wanted += [(w, "reference", {**settings, anchor["setting_id"]: str(anchor_value)}) for w in windows]
        else:
            derived = conn.execute("SELECT DISTINCT window_id, rung FROM arm_ladder_rung WHERE arm_id = ? ORDER BY window_id, rung", (arm_id,)).fetchall()
            wanted += [(w, "reference", {**settings, anchor["setting_id"]: str(r)}) for w, r in derived if w in windows]   # a derived ladder has its own rungs; --rungs is the base arm's
    rid = _new_run_id("encode", search_id, now)
    plan = RunPlan(run_id=rid, stage="encode", host=host, node_label=node_label(host, unit_id), encoder_unit_id=unit_id,
                   content_class_id=search["content_class_id"], search_id=search_id, windows=tuple(windows), planned_at=now.isoformat(), **artifact.pins(identity))
    planned, specs, inputs, skipped, device = _encode_cells(conn, host_row, identity, unit_id, cc["reference_set_id"], rid, "encode", wanted)
    if not planned:
        raise Refusal(f"nothing to encode: every cell of {search_id} on {host} exists already", "a cell is planned once; abandon and re-plan only what failed")
    plan = _with_cells(plan, planned)
    return plan, _body(_run_section(plan, host_row, identity, device=device), windows=windows, inputs=inputs, cells=specs, skipped=skipped)


def plan_time(conn, run_id, now, lane=None, repeats=5):
    """Stage 7: every configuration the source run encoded, on the source cut, through the lane's production chain."""
    source = _row(conn, "run", run_id=run_id)
    if source["stage"] not in ingest.ENCODING_STAGES or source["content_class_id"] is None:
        raise Refusal(f"run {run_id} is {source['stage']}, not an encoding run of a class", "time an encode, screen or viewing run")
    host_row = _row(conn, "host", host=source["host"])
    identity = _identity(conn, source["host"])
    lanes = _served_lanes(conn, source["content_class_id"])
    if lane is None:
        if len(lanes) != 1:
            raise Refusal(f"{source['content_class_id']} serves {', '.join(lanes)}", "time --lane names which chain to time")
        lane = lanes[0]
    chain = _row(conn, "chain", lane=lane, host=source["host"], encoder_unit_id=source["encoder_unit_id"])
    cc = _row(conn, "content_class", content_class_id=source["content_class_id"])
    windows = [w for (w,) in conn.execute("SELECT window_id FROM run_window WHERE run_id = ? ORDER BY window_id", (run_id,))]
    configurations = []
    for (key,) in conn.execute("SELECT cell_key FROM cell WHERE run_id = ? ORDER BY cell_key", (run_id,)):
        ident = dict(conn.execute("SELECT setting_id, value FROM cell_setting WHERE cell_key = ? AND role = 'identity'", (key,)).fetchall())
        if ident not in configurations:
            configurations.append(ident)
    wanted = [(w, "source", ident) for w in windows for ident in configurations]
    rid = _new_run_id("time", run_id, now)
    plan = RunPlan(run_id=rid, stage="time", host=source["host"], node_label=node_label(source["host"], source["encoder_unit_id"]),
                   encoder_unit_id=source["encoder_unit_id"], content_class_id=source["content_class_id"], search_id=source["search_id"],
                   windows=tuple(windows), planned_at=now.isoformat(), **artifact.pins(identity))
    planned, specs, inputs, skipped, device = _encode_cells(conn, host_row, identity, source["encoder_unit_id"], cc["reference_set_id"], rid, "time",
                                                            wanted, repeats=repeats, chain=chain)
    if not planned:
        raise Refusal(f"nothing to time: every configuration of {run_id} on its source cuts exists already", "a cell is planned once")
    plan = _with_cells(plan, planned)
    return plan, _body(_run_section(plan, host_row, identity, device=device, chain={"lane": lane, "vf_template": chain["vf_template"]}),
                       windows=windows, inputs=inputs, cells=specs, skipped=skipped)


# ---------------------------------------------------------------- scoring

def plan_score(conn, run_id, now, scorer=None, keep=False):
    """Stage 6: the parent's kept encodes, at the search's height or the served lane's, on the scorer of the parent's machine
    unless one is named; files on another machine come from the share, and only what was published is there."""
    parent = _row(conn, "run", run_id=run_id)
    if parent["stage"] not in ingest.ENCODING_STAGES:
        raise Refusal(f"run {run_id} is {parent['stage']}, not an encoding run", "score an encode, screen, locate or viewing run")
    parent_host = _row(conn, "host", host=parent["host"])
    same_machine = conn.execute("SELECT s.host FROM scorer s JOIN host h ON h.host = s.host WHERE h.machine = ? ORDER BY s.host", (parent_host["machine"],)).fetchone()
    same_machine = None if same_machine is None else same_machine[0]
    alternative = same_machine or "a scorer on its machine"
    if scorer is None:
        if same_machine is None:
            raise Refusal(f"no scorer on machine {parent_host['machine']}", "add-scorer on a host of that machine, or --scorer another host")
        scorer = same_machine
    scorer_row = _row(conn, "host", host=scorer)
    tools = _scorer(conn, scorer)
    if tools is None:
        raise Refusal(f"{scorer} has no scorer row", "add-scorer for it, or score on a host that has one")
    identity = _identity(conn, scorer)
    if tools["metric_backend"] not in identity.ffmpeg_filters:
        raise Refusal(f"{scorer}'s ffmpeg build lacks {tools['metric_backend']}, the scorer's metric_backend",
                      "score with the node-score image, or add-scorer with --metric-backend libvmaf")
    height = ingest.score_height(conn, {"run_id": run_id, "search_id": parent["search_id"], "content_class_id": parent["content_class_id"]})
    cc = _row(conn, "content_class", content_class_id=parent["content_class_id"])
    refset = _row(conn, "reference_set", reference_set_id=cc["reference_set_id"])
    native_w, native_h = (int(x) for x in refset["geometry"].split("x"))
    w, h = recipes.geometry(native_w, native_h, height)
    kept = conn.execute("SELECT c.cell_key, c.window_id FROM cell c JOIN encode e ON e.cell_key = c.cell_key AND e.kept = 1 "
                        "WHERE c.run_id = ? ORDER BY c.cell_key", (run_id,)).fetchall()
    if not kept:
        raise Refusal(f"run {run_id} has no kept encode to score", "encode first; a scored encode is discarded, so a re-score needs a re-encode")
    local = scorer_row["machine"] == parent_host["machine"]
    references = {}
    for window_id in sorted({wid for _, wid in kept}):
        cut = _cut(conn, cc["reference_set_id"], window_id, "reference")
        relative = exchange.share_path("cut", reference_set_id=cc["reference_set_id"], window_id=window_id, cut_kind="reference")
        holder = _refset_holder(conn, scorer_row["machine"], window_id)
        if holder is not None:
            path = exchange.work_path(scorer_row, relative) if holder == scorer else exchange.viewed_path(scorer_row, _row(conn, "host", host=holder), relative)
            references[window_id] = {"path": path, "pull": None, "content_sha": cut["content_sha"]}
        else:
            pub = _published(conn, relative)
            if pub is None:
                raise Refusal(f"reference cut {cut['cut_id']} is neither on {scorer_row['machine']} nor on the share",
                              f"publish --cut {cut['cut_id']} first, or score on {alternative}")
            references[window_id] = {"path": exchange.work_path(scorer_row, relative), "pull": pub, "content_sha": cut["content_sha"]}
    cells = []
    for key, window_id in kept:
        relative = exchange.share_path("enc", run_id=run_id, cell_key=key)
        if local:
            path = exchange.work_path(scorer_row, relative) if scorer == parent["host"] else exchange.viewed_path(scorer_row, parent_host, relative)
            encode = {"path": path, "pull": None}
        else:
            pub = _published(conn, relative)
            if pub is None:
                raise Refusal(f"cell {key} of run {run_id} is not on the share", f"publish --run {run_id} first, or score on {alternative}")
            encode = {"path": exchange.work_path(scorer_row, relative), "pull": pub}
        cells.append({"cell_key": key, "window_id": window_id, "encode": encode, "reference": references[window_id]})
    windows = tuple(sorted(references))
    plan = RunPlan(run_id=_new_run_id("score", run_id, now), stage="score", host=scorer, node_label=scorer, parent_run_id=run_id,
                   scorer_build=artifact.scorer_build(identity), ffvship_version=identity.ffvship_version, metric_backend=tools["metric_backend"],
                   windows=windows, planned_at=now.isoformat(), **artifact.pins(identity))
    score = {"height": height, "w": w, "h": h, "keep": keep, "metric_backend": tools["metric_backend"],
             "tools": {"ffvship": tools["ffvship"], "score_ffmpeg": tools["score_ffmpeg"], "gpu_id": tools["gpu_id"]}, "cache_dir": tools["cache_dir"]}
    run = _run_section(plan, scorer_row, identity, tools={"ffmpeg": scorer_row["ffmpeg"], **score["tools"]})
    run.update({"encoder_unit_id": parent["encoder_unit_id"], "content_class_id": parent["content_class_id"], "search_id": parent["search_id"]})
    return plan, _body(run, windows=windows, cells=cells, score=score)


# ---------------------------------------------------------------- the exchange

def plan_publish(conn, run_id=None, cut_ids=(), via=None):
    """(host, job): the files an agent copies to the share -- a run's kept encodes, or cuts -- read through local_view when
    another runtime on the machine does the writing (eta's encodes leave through eta-wsl)."""
    files = []
    if run_id is not None:
        run = _row(conn, "run", run_id=run_id)
        owner = _row(conn, "host", host=run["host"])
        via = via or run["host"]
        via_row = _row(conn, "host", host=via)
        kept = conn.execute("SELECT c.cell_key FROM cell c JOIN encode e ON e.cell_key = c.cell_key AND e.kept = 1 WHERE c.run_id = ? ORDER BY c.cell_key", (run_id,)).fetchall()
        if not kept:
            raise Refusal(f"run {run_id} has no kept encode to publish", "publish a run whose encodes are kept; scoring discards them")
        for (key,) in kept:
            relative = exchange.share_path("enc", run_id=run_id, cell_key=key)
            local = exchange.work_path(owner, relative) if via == run["host"] else exchange.viewed_path(via_row, owner, relative)
            files.append({"local": local, "relative": relative, "run_id": run_id, "cell_key": key, "cut_id": None})
    if cut_ids:
        if via is None:
            raise Refusal("publish --cut needs --via", "name the runtime that holds the reference set and can write the share")
        via_row = _row(conn, "host", host=via)
    for cut_id in cut_ids:
        cut = _row(conn, "cut", cut_id=cut_id)
        relative = exchange.share_path("cut", reference_set_id=cut["reference_set_id"], window_id=cut["window_id"], cut_kind=cut["kind"])
        holder = _refset_holder(conn, via_row["machine"], cut["window_id"])
        if holder is None:
            raise Refusal(f"nothing on machine {via_row['machine']} holds {cut_id}", "materialise --adopt on a host of that machine first")
        local = exchange.work_path(via_row, relative) if holder == via else exchange.viewed_path(via_row, _row(conn, "host", host=holder), relative)
        files.append({"local": local, "relative": relative, "run_id": None, "cell_key": None, "cut_id": cut_id})
    return via, {"kind": "publish", "by_host": via, "files": files}


# ---------------------------------------------------------------- enqueue, done, claim

def enqueue(store, queue, plan, body):
    """The plan rows in one checked transaction (a blocked host, an unquiet machine and every other check refuse here), then
    the queue entry; a queue that fails after the commit leaves the run abandoned and visible, never launched unobserved."""
    with store.transaction() as conn:
        st.plan_run(conn, plan)
        if plan.stage in QUIET_STAGES:
            busy = conn.execute("SELECT r.run_id, r.state, r.host FROM run r JOIN host h ON h.host = r.host "
                                "WHERE h.machine = (SELECT machine FROM host WHERE host = ?) AND r.state IN ('launched', 'running') AND r.run_id <> ?",
                                (plan.host, plan.run_id)).fetchone()
            if busy is not None:
                machine = conn.execute("SELECT machine FROM host WHERE host = ?", (plan.host,)).fetchone()[0]
                raise Refusal(f"machine {machine} is not quiet: {busy[0]} is {busy[1]} on {busy[2]}",
                              "wait for it or abandon it; a timing run runs alone on its machine")
    try:
        return queue.enqueue(plan.host, plan.run_id, body)
    except Exception:
        with store.transaction() as conn:
            st.post_event(conn, plan.run_id, dt.datetime.now(dt.timezone.utc).isoformat(timespec="microseconds"), "abandoned", "queue write failed")
        raise


def done_cells(conn, run, body):
    """What the store already holds of this plan, per stage, so a restarted agent skips it."""
    stage, rid = run["stage"], run["run_id"]
    if stage == "score":
        return {k for (k,) in conn.execute("SELECT DISTINCT cell_key FROM score WHERE run_id = ?", (rid,))}
    if stage in ("time", "split", "concurrency"):
        repeats = {c["cell_key"]: c.get("repeats", 1) for c in body.get("cells", [])}
        done = set()
        for key, n in conn.execute("SELECT cell_key, count(DISTINCT repeat_index) FROM timing WHERE cell_key IN "
                                   "(SELECT cell_key FROM cell WHERE run_id = ?) GROUP BY cell_key", (rid,)):
            if n >= repeats.get(key, 1):
                done.add(key)
        return done
    if stage == "inventory":
        ids = [i["title_id"] for i in body.get("inputs", [])]
        return {t for (t,) in conn.execute(f"SELECT title_id FROM title WHERE title_id IN ({','.join('?' * len(ids))})", ids)} if ids else set()
    if stage == "materialise":
        ids = [c["cut_id"] for c in body.get("cuts", [])]
        return {c for (c,) in conn.execute(f"SELECT cut_id FROM cut WHERE cut_id IN ({','.join('?' * len(ids))})", ids)} if ids else set()
    return {k for (k,) in conn.execute("SELECT c.cell_key FROM cell c WHERE c.run_id = ? AND (EXISTS (SELECT 1 FROM encode e WHERE e.cell_key = c.cell_key) "
                                       "OR EXISTS (SELECT 1 FROM cell_failure f WHERE f.cell_key = c.cell_key))", (rid,))}


def claim_body(conn, entry):
    """The entry's plan plus what is already recorded: a resume is idempotent because a cell with a record is skipped."""
    body = dict(entry.plan)
    if entry.kind == "publish":
        body["done"] = sorted(f["relative"] for f in body["files"] if _published(conn, f["relative"]) is not None)
        return body
    run = _row(conn, "run", run_id=entry.run_id)
    body["done"] = sorted(done_cells(conn, run, body))
    return body
