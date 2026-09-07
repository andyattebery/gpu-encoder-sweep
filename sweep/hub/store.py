"""sweep/hub/store.py -- one SQLite connection behind a lock; every write is one transaction that runs every check.

The schema is sweep/schema.sql, loaded into an empty store and pinned by PRAGMA user_version (a hash of the schema
text), so a store opened under another schema refuses instead of half-working. Stdlib only.
"""
import hashlib
import re
import sqlite3
import threading
from collections import OrderedDict
from contextlib import contextmanager
from dataclasses import dataclass

from sweep import model_check as mc
from sweep.hub.refusals import ADD_VERB, Refusal, check_refusal, integrity_refusal


def schema_version(schema):
    return int(hashlib.sha256(schema.encode()).hexdigest()[:7], 16)


class Store:
    def __init__(self, path=":memory:"):
        self.path = path
        self.lock = threading.RLock()
        schema = mc.SCHEMA.read_text()
        self.version = schema_version(schema)
        self.conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.conn.execute("PRAGMA foreign_keys = ON")
        if path != ":memory:":
            self.conn.execute("PRAGMA journal_mode = WAL")
        if not mc.db_tables(self.conn):
            self.conn.executescript(schema)
            self.conn.execute(f"PRAGMA user_version = {self.version}")
        else:
            (current,) = self.conn.execute("PRAGMA user_version").fetchone()
            if current != self.version:
                self.conn.close()
                raise Refusal(f"{path} was created under another schema (user_version {current}; this schema is {self.version})",
                              "migrate the store, or open it with the schema it was created under")
        self.tags, self.checks = mc.parse_tags(schema)

    def close(self):
        self.conn.close()

    @contextmanager
    def transaction(self):
        """One write: BEGIN IMMEDIATE, the body, every check; a firing check or an integrity error rolls back and refuses."""
        with self.lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                yield self.conn
                firing = [(k, v) for k, v in mc.run_checks(self.conn, self.tags).items() if v]
                if firing:
                    name, rows = firing[0]
                    text = self.checks[name] if name in self.checks else dict(zip(("check", "fix"), mc.SCRIPT_CHECKS[name]))
                    raise check_refusal(name, text["check"], rows, text["fix"], [k for k, _ in firing[1:]])
            except sqlite3.IntegrityError as e:
                self.conn.execute("ROLLBACK")
                raise integrity_refusal(e) from e
            except BaseException:
                self.conn.execute("ROLLBACK")
                raise
            self.conn.execute("COMMIT")

    @contextmanager
    def reading(self):
        with self.lock:
            yield self.conn

    def check(self):
        """{check: its rows} for the checks that fire; empty on a valid store."""
        with self.lock:
            return OrderedDict((k, v) for k, v in mc.run_checks(self.conn, self.tags).items() if v)

    def rows(self, table):
        """Every row of a table as a dict, in primary-key order, columns in schema order."""
        cols = mc.columns(self.conn, table)
        pk = [c["name"] for c in sorted((c for c in cols if c["pk"]), key=lambda c: c["pk"])]
        order = f" ORDER BY {', '.join(pk)}" if pk else ""
        with self.lock:
            return [OrderedDict(zip([c["name"] for c in cols], r)) for r in self.conn.execute(f"SELECT * FROM {table}{order}")]


# ---------------------------------------------------------------- writing rows: the verbs' vocabulary

def _ident(name):
    if not re.fullmatch(r"\w+", name):
        raise ValueError(f"not an identifier: {name!r}")
    return name


def require(conn, table, **key):
    """The referenced row exists, by name; otherwise the refusal names it and the verb that creates it."""
    where = " AND ".join(f"{_ident(k)} = ?" for k in key)
    if conn.execute(f"SELECT 1 FROM {_ident(table)} WHERE {where}", tuple(key.values())).fetchone() is None:
        named = ", ".join(f"{k}={v!r}" for k, v in key.items())
        raise Refusal(f"{table} {named} does not exist", f"add it first with {ADD_VERB.get(table, 'its verb')}")


def insert(conn, table, row):
    cols = [_ident(c) for c in row]
    conn.execute(f"INSERT INTO {_ident(table)} ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})", tuple(row.values()))


def insert_returning_id(conn, table, row, id_column):
    cols = [_ident(c) for c in row]
    (new_id,) = conn.execute(f"INSERT INTO {_ident(table)} ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))}) "
                             f"RETURNING {_ident(id_column)}", tuple(row.values())).fetchone()
    return new_id


def insert_cut_check(conn, cut_id, check_name, result, reason, checked_at):
    """A content check's row; a classified result carries the reason it is kept (the DDL refuses it without one too)."""
    require(conn, "cut", cut_id=cut_id)
    if result == "classified" and not reason:
        raise Refusal(f"cut {cut_id!r} classified on {check_name} with no reason", "give --reason: what the check found and why the cut is kept")
    insert(conn, "cut_check", {"cut_id": cut_id, "check_name": check_name, "result": result, "reason": reason, "checked_at": checked_at})


# ---------------------------------------------------------------- runs: the plan is rows, the state is its events

@dataclass(frozen=True)
class CellPlan:
    cell_key: str
    window_id: str
    cut_kind: str
    settings: tuple = ()          # (setting_id, value, role) triples


@dataclass(frozen=True)
class RunPlan:
    run_id: str
    stage: str
    host: str
    node_label: str
    artifact: str
    ffmpeg_build: str
    ffmpeg_sha: str
    harness_version: str
    planned_at: str               # the run's started_at until the claim posts launched
    encoder_unit_id: str = None
    content_class_id: str = None
    search_id: str = None
    parent_run_id: str = None     # a score run's; its unit, class, search and windows are copied from it when not given
    scorer_build: str = None
    ffvship_version: str = None
    metric_backend: str = None
    windows: tuple = ()
    cells: tuple = ()             # CellPlans


def plan_run(conn, plan):
    """The run row (planned), its windows, its cells and their settings, and the planned event by the hub."""
    require(conn, "host", host=plan.host)
    unit, cc, search, windows = plan.encoder_unit_id, plan.content_class_id, plan.search_id, list(plan.windows)
    if plan.parent_run_id is not None:
        require(conn, "run", run_id=plan.parent_run_id)
        p_unit, p_cc, p_search = conn.execute("SELECT encoder_unit_id, content_class_id, search_id FROM run WHERE run_id = ?",
                                              (plan.parent_run_id,)).fetchone()
        unit, cc, search = unit or p_unit, cc or p_cc, search or p_search
        if not windows:
            windows = [w for (w,) in conn.execute("SELECT window_id FROM run_window WHERE run_id = ? ORDER BY window_id", (plan.parent_run_id,))]
    if unit is not None:
        require(conn, "encoder_unit", encoder_unit_id=unit)
    if cc is not None:
        require(conn, "content_class", content_class_id=cc)
    if search is not None:
        require(conn, "search", search_id=search)
    insert(conn, "run", {
        "run_id": plan.run_id, "encoder_unit_id": unit, "content_class_id": cc, "search_id": search,
        "parent_run_id": plan.parent_run_id, "host": plan.host, "node_label": plan.node_label, "stage": plan.stage,
        "artifact": plan.artifact, "ffmpeg_build": plan.ffmpeg_build, "ffmpeg_sha": plan.ffmpeg_sha,
        "scorer_build": plan.scorer_build, "ffvship_version": plan.ffvship_version, "metric_backend": plan.metric_backend,
        "harness_version": plan.harness_version, "started_at": plan.planned_at, "state": "planned"})
    for w in windows:
        require(conn, "window", window_id=w)
        insert(conn, "run_window", {"run_id": plan.run_id, "window_id": w})
    for cell in plan.cells:
        insert(conn, "cell", {"cell_key": cell.cell_key, "run_id": plan.run_id, "window_id": cell.window_id, "cut_kind": cell.cut_kind})
        for setting_id, value, role in cell.settings:
            insert(conn, "cell_setting", {"cell_key": cell.cell_key, "setting_id": setting_id, "value": value, "role": role})
    insert(conn, "run_event", {"run_id": plan.run_id, "at": plan.planned_at, "state": "planned", "detail": "planned", "by": "hub"})


def post_event(conn, run_id, at, state, detail=None, by="hub"):
    """Append the event and set the run's state to it; launched stamps started_at, a terminal state finished_at,
    and complete also fetched_at and verified_at (the hub posts it after verifying against the plan)."""
    require(conn, "run", run_id=run_id)
    insert(conn, "run_event", {"run_id": run_id, "at": at, "state": state, "detail": detail, "by": by})
    stamps = {"launched": ["started_at"], "complete": ["finished_at", "fetched_at", "verified_at"],
              "failed": ["finished_at"], "abandoned": ["finished_at"]}.get(state, [])
    sets = ", ".join(["state = ?"] + [f"{c} = ?" for c in stamps])
    conn.execute(f"UPDATE run SET {sets} WHERE run_id = ?", (state, *([at] * len(stamps)), run_id))


def calibrate(conn, name, run_id, value, computed_at):
    """A measured constant's value, from the calibrate run that computed it; the only writer of constant_value."""
    require(conn, "constant", name=name)
    require(conn, "run", run_id=run_id)
    (provenance,) = conn.execute("SELECT provenance FROM constant WHERE name = ?", (name,)).fetchone()
    if provenance != "measured":
        raise Refusal(f"{name} is {provenance}, not measured",
                      "calibrate writes measured constants only; a derived or policy value is authored with add-constant")
    insert(conn, "constant_value", {"name": name, "run_id": run_id, "value": value, "computed_at": computed_at})
