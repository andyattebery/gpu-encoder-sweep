"""sweep/hub/ingest.py -- a posted record reaches its ROW table under the run that claimed it.

A record names a cell its run planned (a score run: its parent's), is of a kind the run's stage produces, and is
written once: an identical repost is a no-op, a differing one is refused. The caller owns the transaction, so a
run's records go through one check pass. In M2 these models become the `/runs/{id}/records` body.
"""
from typing import Annotated, Literal, Union

from pydantic import Field, TypeAdapter

from sweep.hub import store as st
from sweep.hub.api.common import Body
from sweep.hub.refusals import Refusal


class EncodeRecord(Body):
    kind: Literal["encode"]
    cell_key: str
    bytes: int
    bitrate_kbps: float
    frames: int
    duration_s: float
    decode_path: str
    kept: bool                    # an encode-stage encode is kept until its score record discards it


class FailureRecord(Body):
    kind: Literal["failure"]
    cell_key: str
    at: str
    stderr: str                   # verbatim; the exit status is recorded, never trusted
    rc: int | None = None


class ScoreValue(Body):
    metric: str
    statistic: str
    value: float


class StepTime(Body):
    scoring_step: str
    seconds: float
    cores_busy: float | None = None
    gpu_mean: float | None = None
    gpu_max: float | None = None


class ScoreRecord(Body):
    kind: Literal["score"]
    cell_key: str
    recipe: str
    kept: bool = False            # the one second writer of encode.kept: scoring discards
    scores: list[ScoreValue]
    steps: list[StepTime] = []


class TimingSample(Body):
    workers: int
    repeat_index: int
    fps: float
    wall_s: float
    decode_path: str
    is_warmup: bool
    noise_floor_pct: float | None = None
    leg: str = "full"
    frames: int


class TimingRecord(Body):
    kind: Literal["timing"]
    cell_key: str
    samples: list[TimingSample]


class ScreenVerdictRecord(Body):
    kind: Literal["screen_verdict"]
    setting_id: str
    verdict: str
    magnitude_pct: float | None = None
    base_setting_id: str | None = None
    base_value: str | None = None
    noise_floor_pct: float | None = None
    reason: str | None = None
    cells: list[str]              # the encodes it summarises, on one window


class AdmissibilityVerdictRecord(Body):
    kind: Literal["admissibility_verdict"]
    setting_id: str
    test: str
    base_setting_id: str | None = None
    base_value: str | None = None
    verdict: str
    reason: str | None = None
    cells: list[str]


class TitleRecord(Body):
    kind: Literal["title"]
    title_id: str
    path: str
    library: str
    width: int
    height: int
    dynamic_range: str
    dv_profile: int | None = None
    video_codec: str
    field_order: str
    fps: float
    bit_depth: int
    bitrate_kbps: float
    bpp: float
    source_type: str
    audio_layout: str | None = None
    subtitle_layout: str | None = None
    scanned_at: str


class ReferenceSetRecord(Body):
    kind: Literal["reference_set"]
    reference_set_id: str
    geometry: str
    pix_fmt: str
    built_with: str
    built_at: str


class CutRecord(Body):
    kind: Literal["cut"]
    cut_id: str
    reference_set_id: str
    window_id: str
    cut_kind: str                 # reference | source (the column is `kind`; this record's kind is `cut`)
    chain_lane: str | None = None
    chain_host: str | None = None
    chain_unit: str | None = None
    content_sha: str
    bytes: int
    frames: int
    tags_pinned: str | None = None


class CutCheckRecord(Body):
    kind: Literal["cut_check"]
    cut_id: str
    check_name: str
    result: str
    reason: str | None = None
    checked_at: str


Record = Annotated[Union[EncodeRecord, FailureRecord, ScoreRecord, TimingRecord, ScreenVerdictRecord, AdmissibilityVerdictRecord,
                         TitleRecord, ReferenceSetRecord, CutRecord, CutCheckRecord], Field(discriminator="kind")]
_records = TypeAdapter(Record)


def parse_record(data):
    return _records.validate_python(data)


ENCODING_STAGES = frozenset({"screen", "locate", "encode", "time", "split", "concurrency", "viewing", "probe", "calibrate"})
STAGES_FOR_KIND = {
    "encode": ENCODING_STAGES, "failure": ENCODING_STAGES, "score": frozenset({"score"}),
    "timing": frozenset({"time", "split", "concurrency"}), "screen_verdict": frozenset({"screen"}),
    "admissibility_verdict": frozenset({"screen"}), "title": frozenset({"inventory"}),
    "reference_set": frozenset({"materialise"}), "cut": frozenset({"materialise"}), "cut_check": frozenset({"verify"}),
}
NEVER_OVERWRITTEN = "a record is never overwritten; abandon the run and re-plan"


def ingest_record(conn, run_id, record):
    st.require(conn, "run", run_id=run_id)
    run = dict(zip(("run_id", "stage", "parent_run_id", "search_id", "host", "encoder_unit_id", "scorer_build"),
                   conn.execute("SELECT run_id, stage, parent_run_id, search_id, host, encoder_unit_id, scorer_build FROM run WHERE run_id = ?",
                                (run_id,)).fetchone()))
    stages = STAGES_FOR_KIND[record.kind]
    if run["stage"] not in stages:
        raise Refusal(f"{_an(record.kind)} {record.kind} record under {_an(run['stage'])} {run['stage']} run",
                      f"{record.kind} records come from {', '.join(sorted(stages))} runs")
    INGEST[record.kind](conn, run, record)


def _an(word):
    return "an" if word[0] in "aeiou" else "a"


def _planned(conn, run, cell_key):
    """The cell belongs to the run's plan (a score run scores its parent's cells)."""
    owner = run["parent_run_id"] if run["stage"] == "score" else run["run_id"]
    row = conn.execute("SELECT run_id FROM cell WHERE cell_key = ?", (cell_key,)).fetchone()
    if row is None or row[0] != owner:
        raise Refusal(f"cell {cell_key!r} is not planned in run {run['run_id']!r}", "a record names a cell its run planned")


def _write_once(conn, table, key, row, what, fix=NEVER_OVERWRITTEN):
    """Insert the row, or do nothing when the same row is there, or refuse when a differing one is."""
    where = " AND ".join(f"{k} = ?" for k in key)
    existing = conn.execute(f"SELECT {', '.join(row)} FROM {table} WHERE {where}", tuple(key.values())).fetchone()
    if existing is None:
        st.insert(conn, table, row)
    elif tuple(existing) != tuple(row.values()):
        raise Refusal(what, fix)


def _ingest_encode(conn, run, r):
    _planned(conn, run, r.cell_key)
    row = {"cell_key": r.cell_key, "bytes": r.bytes, "bitrate_kbps": r.bitrate_kbps, "frames": r.frames, "duration_s": r.duration_s,
           "decode_path": r.decode_path, "kept": int(r.kept)}
    _write_once(conn, "encode", {"cell_key": r.cell_key}, row, f"cell {r.cell_key!r} already has an encode record that differs")


def _ingest_failure(conn, run, r):
    _planned(conn, run, r.cell_key)
    row = {"cell_key": r.cell_key, "at": r.at, "stderr": r.stderr, "rc": r.rc}
    _write_once(conn, "cell_failure", {"cell_key": r.cell_key}, row, f"cell {r.cell_key!r} already has a failure record that differs")


def _ingest_score(conn, run, r):
    _planned(conn, run, r.cell_key)
    if run["search_id"] is None:
        raise Refusal(f"score run {run['run_id']!r} has no search to take the height from",
                      "score runs are planned from a search's encoding run; the height is the search's")
    if run["scorer_build"] is None:
        raise Refusal(f"score run {run['run_id']!r} names no scorer build", "plan a score run with the scorer's build, from the artifact the agent reports")
    (height,) = conn.execute("SELECT score_height FROM search WHERE search_id = ?", (run["search_id"],)).fetchone()
    want = {(height, s.metric, s.statistic, s.value, r.recipe, run["scorer_build"]) for s in r.scores}
    existing = set(conn.execute("SELECT height, metric, statistic, value, recipe, scorer_build FROM score WHERE run_id = ? AND cell_key = ?",
                                (run["run_id"], r.cell_key)).fetchall())
    if existing:
        if existing == want:
            return
        raise Refusal(f"cell {r.cell_key!r} already has a score record that differs", NEVER_OVERWRITTEN)
    for height_, metric, statistic, value, recipe, build in sorted(want):
        st.insert(conn, "score", {"run_id": run["run_id"], "cell_key": r.cell_key, "height": height_, "metric": metric, "statistic": statistic,
                                  "value": value, "recipe": recipe, "scorer_build": build})
    for step in r.steps:
        st.insert(conn, "step_trace", dict(run_id=run["run_id"], cell_key=r.cell_key, height=height, **step.model_dump()))
    conn.execute("UPDATE encode SET kept = ? WHERE cell_key = ?", (int(r.kept), r.cell_key))


def _ingest_timing(conn, run, r):
    _planned(conn, run, r.cell_key)
    cols = ("workers", "repeat_index", "fps", "wall_s", "frames", "decode_path", "is_warmup", "noise_floor_pct", "leg")
    want = {(s.workers, s.repeat_index, s.fps, s.wall_s, s.frames, s.decode_path, int(s.is_warmup), s.noise_floor_pct, s.leg) for s in r.samples}
    existing = set(conn.execute(f"SELECT {', '.join(cols)} FROM timing WHERE cell_key = ?", (r.cell_key,)).fetchall())
    if existing:
        if existing == want:
            return
        raise Refusal(f"cell {r.cell_key!r} already has a timing record that differs", NEVER_OVERWRITTEN)
    for sample in sorted(want):
        st.insert(conn, "timing", dict(cell_key=r.cell_key, **dict(zip(cols, sample))))


def _one_window(conn, run, cells):
    """The verdict's cells are the run's, and lie on one window: a verdict is per window."""
    if not cells:
        raise Refusal("a verdict with no cells", "the screen posts a verdict with the cells it summarises")
    windows = set()
    for cell_key in cells:
        _planned(conn, run, cell_key)
        windows.add(conn.execute("SELECT window_id FROM cell WHERE cell_key = ?", (cell_key,)).fetchone()[0])
    if len(windows) != 1:
        raise Refusal("a screen verdict over cells on more than one window", "a verdict is per window; post one per window")
    return windows.pop()


def _verdict(conn, run, r, table, id_column, cell_table, extra):
    """A screen or admissibility verdict with its cells, written once per its UNIQUE key."""
    window = _one_window(conn, run, r.cells)
    key = {"encoder_unit_id": run["encoder_unit_id"], "setting_id": r.setting_id, "window_id": window,
           "base_setting_id": r.base_setting_id, "base_value": r.base_value, **extra}
    values = {k: getattr(r, k) for k in ("verdict", "magnitude_pct", "noise_floor_pct", "reason") if hasattr(r, k)}
    where = " AND ".join(f"{k} IS ?" for k in key)
    existing = conn.execute(f"SELECT {id_column}, {', '.join(values)} FROM {table} WHERE {where}", tuple(key.values())).fetchone()
    if existing is not None:
        cells = {c for (c,) in conn.execute(f"SELECT cell_key FROM {cell_table} WHERE {id_column} = ?", (existing[0],))}
        if tuple(existing[1:]) == tuple(values.values()) and cells == set(r.cells):
            return
        raise Refusal(f"a verdict on {r.setting_id} at {window} already exists and differs", NEVER_OVERWRITTEN)
    new_id = st.insert_returning_id(conn, table, {**key, **values}, id_column)
    for cell_key in r.cells:
        st.insert(conn, cell_table, {id_column: new_id, "cell_key": cell_key})


def _ingest_screen_verdict(conn, run, r):
    _verdict(conn, run, r, "setting_verdict", "verdict_id", "setting_verdict_cell", {})


def _ingest_admissibility_verdict(conn, run, r):
    _verdict(conn, run, r, "admissibility_verdict", "admissibility_id", "admissibility_verdict_cell", {"test": r.test})


def _ingest_title(conn, run, r):
    """The population: a rescan updates a title's row, found by its path; the id is stable."""
    row = r.model_dump(exclude={"kind"})
    existing = conn.execute("SELECT title_id FROM title WHERE path = ?", (r.path,)).fetchone()
    if existing is None:
        st.insert(conn, "title", row)
    elif existing[0] != r.title_id:
        raise Refusal(f"path {r.path!r} is title {existing[0]!r}, not {r.title_id!r}", "a title's id is stable across scans")
    else:
        sets = ", ".join(f"{k} = ?" for k in row if k != "title_id")
        conn.execute(f"UPDATE title SET {sets} WHERE title_id = ?", tuple(v for k, v in row.items() if k != "title_id") + (r.title_id,))


def _ingest_reference_set(conn, run, r):
    row = dict(r.model_dump(exclude={"kind"}), built_on=run["host"])
    _write_once(conn, "reference_set", {"reference_set_id": r.reference_set_id}, row,
                f"reference set {r.reference_set_id!r} already has a record that differs")


def _ingest_cut(conn, run, r):
    row = r.model_dump(exclude={"kind", "cut_kind"})
    row = {"cut_id": r.cut_id, "reference_set_id": r.reference_set_id, "window_id": r.window_id, "kind": r.cut_kind, **{k: v for k, v in row.items() if k not in ("cut_id", "reference_set_id", "window_id")}}
    _write_once(conn, "cut", {"cut_id": r.cut_id}, row, f"cut {r.cut_id!r} already has a record that differs",
                "a record is never overwritten; a cut is re-materialised only by content hash")


def _ingest_cut_check(conn, run, r):
    existing = conn.execute("SELECT result, reason FROM cut_check WHERE cut_id = ? AND check_name = ? AND checked_at = ?",
                            (r.cut_id, r.check_name, r.checked_at)).fetchone()
    if existing is None:
        st.insert_cut_check(conn, r.cut_id, r.check_name, r.result, r.reason, r.checked_at)
    elif tuple(existing) != (r.result, r.reason):
        raise Refusal(f"cut {r.cut_id!r} already has a {r.check_name} check at {r.checked_at} that differs", NEVER_OVERWRITTEN)


INGEST = {"encode": _ingest_encode, "failure": _ingest_failure, "score": _ingest_score, "timing": _ingest_timing,
          "screen_verdict": _ingest_screen_verdict, "admissibility_verdict": _ingest_admissibility_verdict, "title": _ingest_title,
          "reference_set": _ingest_reference_set, "cut": _ingest_cut, "cut_check": _ingest_cut_check}
