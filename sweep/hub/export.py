"""sweep/hub/export.py -- the record as files, so a change is a diff someone reviews.

Every table has a home: FILE tables under authored/, the sample's ROW tables under sample/, what the agents
reported outside any run under agents/, a run's plan, events and records under runs/<run_id>/, the calibrated
constants in constants.json, and the API's own description in openapi.json. Rows are in primary-key order and keys are sorted, so the same store renders the same bytes and a
changed row changes one file. `write` removes what it wrote last time and nothing else. Stdlib only.
"""
import json
from collections import OrderedDict
from pathlib import Path

from sweep import model_check as mc

# table -> (home, how a row finds its run); the FILE tables are authored/, read off the schema's tags
EXPORT_HOMES = {
    **{t: ("authored", None) for t, tg in mc.parse_tags()[0].items() if tg["class"] == "FILE"},
    # the sample's ROW tables: no run column, one file each
    "title": ("sample", None), "reference_set": ("sample", None), "cut": ("sample", None), "cut_check": ("sample", None),
    # a run's plan: one object per run
    "run": ("plan", None), "run_window": ("plan", None), "cell": ("plan", None), "cell_setting": ("plan", None),
    "run_event": ("events", None),
    # a run's records, by the column that names the run, or through the cell that does
    "encode": ("record", "cell"), "cell_failure": ("record", "cell"), "timing": ("record", "cell"),
    "score": ("record", "run_id"), "step_trace": ("record", "run_id"), "arm_ladder_rung": ("record", "run_id"),
    "setting_verdict": ("record", "verdict_cells"), "admissibility_verdict": ("record", "verdict_cells"),
    "setting_verdict_cell": ("nested", None), "admissibility_verdict_cell": ("nested", None),
    "scorer_equivalence": ("record", "run_a"),
    "constant_value": ("constants", None),
    # what the agents reported outside any run: identities (and, from M2, publishes)
    "host_identity": ("agents", None),
}
MANAGED = ("authored", "sample", "agents", "runs", "constants.json", "openapi.json")


def dumps(obj):
    return json.dumps(obj, sort_keys=True, indent=2, ensure_ascii=False) + "\n"


def _columns(conn, table):
    cols = mc.columns(conn, table)
    pk = [c["name"] for c in sorted((c for c in cols if c["pk"]), key=lambda c: c["pk"])]
    return [c["name"] for c in cols], pk


def rows(conn, table, where="", params=()):
    names, pk = _columns(conn, table)
    order = f" ORDER BY {', '.join(pk)}" if pk else ""
    return [dict(zip(names, r)) for r in conn.execute(f"SELECT {', '.join(names)} FROM {table}{where}{order}", params)]


def _run_of_cell(conn):
    return {ck: run for ck, run in conn.execute("SELECT cell_key, run_id FROM cell")}


def _by_run(conn, table, how, cell_runs):
    """{run_id: [rows]} for a record table, by the rule its home names."""
    out = {}
    if how == "run_id" or how == "run_a":
        col = "run_id" if how == "run_id" else "run_a"
        for r in rows(conn, table):
            out.setdefault(r[col], []).append(r)
    elif how == "cell":
        for r in rows(conn, table):
            out.setdefault(cell_runs[r["cell_key"]], []).append(r)
    elif how == "verdict_cells":
        id_col = "verdict_id" if table == "setting_verdict" else "admissibility_id"
        cell_table = "setting_verdict_cell" if table == "setting_verdict" else "admissibility_verdict_cell"
        cells = {}
        for r in rows(conn, cell_table):
            cells.setdefault(r[id_col], []).append(r["cell_key"])
        for r in rows(conn, table):
            r["cells"] = sorted(cells.get(r[id_col], []))
            run = cell_runs[r["cells"][0]] if r["cells"] else "unattributed"
            out.setdefault(run, []).append(r)
    return out


def render(conn, openapi):
    """{path: text} for the whole record, paths sorted, every table in its home."""
    files = {}
    tables = mc.db_tables(conn)
    for t in tables:
        home, how = EXPORT_HOMES[t]
        if home in ("authored", "sample", "agents"):
            files[f"{home}/{t}.json"] = dumps(rows(conn, t))
    cell_runs = _run_of_cell(conn)
    windows, cells, settings = {}, {}, {}
    for r in rows(conn, "run_window"):
        windows.setdefault(r["run_id"], []).append(r["window_id"])
    for r in rows(conn, "cell_setting"):
        settings.setdefault(r["cell_key"], []).append(r)
    for r in rows(conn, "cell"):
        cells.setdefault(r["run_id"], []).append(dict(r, settings=settings.get(r["cell_key"], [])))
    events = {}
    for r in rows(conn, "run_event"):
        events.setdefault(r["run_id"], []).append(r)
    for run in rows(conn, "run"):
        rid = run["run_id"]
        files[f"runs/{rid}/plan.json"] = dumps({"run": run, "windows": sorted(windows.get(rid, [])), "cells": cells.get(rid, [])})
        files[f"runs/{rid}/events.jsonl"] = "".join(json.dumps(e, sort_keys=True, ensure_ascii=False) + "\n" for e in events.get(rid, []))
    for t in tables:
        home, how = EXPORT_HOMES[t]
        if home == "record":
            for rid, rs in _by_run(conn, t, how, cell_runs).items():
                files[f"runs/{rid}/records/{t}.json"] = dumps(rs)
    files["constants.json"] = dumps(rows(conn, "constant_value"))
    files["openapi.json"] = dumps(openapi)
    return OrderedDict(sorted(files.items()))


def write(files, into):
    """Write every file under `into`, and remove what is under the managed homes but not in `files`. Returns the paths written."""
    into = Path(into)
    for rel, text in files.items():
        path = into / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    keep = set(files)
    for home in MANAGED:
        p = into / home
        if p.is_file() and home not in keep:
            p.unlink()
        elif p.is_dir():
            for f in sorted(p.rglob("*"), reverse=True):
                rel = str(f.relative_to(into))
                if f.is_file() and rel not in keep:
                    f.unlink()
                elif f.is_dir() and not any(f.iterdir()):
                    f.rmdir()
    return list(files)
