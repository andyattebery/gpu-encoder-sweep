#!/usr/bin/env python3
"""sweep/hub/ingest.py: a posted record reaches its ROW table under the run that claimed it; a record for a cell the
run never planned, of a kind its stage does not produce, or differing from one already posted, is refused; an
identical repost is a no-op.

    python3 -m unittest tests.test_hub_ingest
"""
import unittest

from sweep.hub import ingest, store
from sweep.hub.refusals import Refusal
from tests.hub_helpers import fixture_store

B580 = "intel-b580-ihd26.2.2-qsv-av1"
NODE = dict(artifact="node-encode:0.0.2.dev0+g0", ffmpeg_build="8.1.2-Jellyfin", ffmpeg_sha="0b0ea2d", harness_version="g0", planned_at="2026-09-05T09:00")
SCORER = dict(NODE, artifact="node-score:0.0.2.dev0+g0")     # the pair media-01-score reported; a run names what its host reported
ARM_A = (("qsv.preset", "4", "identity"), ("qsv.b_strategy", "0", "identity"), ("qsv.q", "24", "identity"))
ENCODE = {"kind": "encode", "cell_key": "c-new", "bytes": 60000000, "bitrate_kbps": 8000.0, "frames": 1439, "duration_s": 60.0,
          "decode_path": "hardware", "kept": True}


class Ingest(unittest.TestCase):
    def setUp(self):
        self.store = fixture_store()
        self.addCleanup(self.store.close)

    def rows(self, table, **key):
        return [r for r in self.store.rows(table) if all(r[k] == v for k, v in key.items())]

    def plan(self, run_id, stage, host="media-01", unit=B580, cc="native-1080p-sdr", search="b580-qsv-av1", windows=("tng",), cells=(), pins=NODE, **more):
        plan = store.RunPlan(run_id=run_id, stage=stage, host=host, node_label=host, encoder_unit_id=unit, content_class_id=cc,
                             search_id=search, windows=windows, cells=cells, **pins, **more)
        with self.store.transaction() as conn:
            store.plan_run(conn, plan)

    def post(self, run_id, *records):
        with self.store.transaction() as conn:
            for data in records:
                ingest.ingest_record(conn, run_id, ingest.parse_record(data))

    def refused(self, run_id, data, text):
        with self.assertRaises(Refusal) as cm:
            self.post(run_id, data)
        self.assertEqual(str(cm.exception), text)

    def encode_run(self):
        self.plan("r2", "encode", cells=(store.CellPlan("c-new", "tng", "reference", ARM_A),))

    # ---- encode, failure
    def test_encode_record_reaches_the_encode_table(self):
        self.encode_run()
        self.post("r2", ENCODE)
        self.assertEqual(self.rows("encode", cell_key="c-new")[0]["kept"], 1)
        self.assertEqual(self.store.check(), {})

    def test_failure_record_reaches_cell_failure(self):
        self.encode_run()
        self.post("r2", {"kind": "failure", "cell_key": "c-new", "at": "2026-09-05T10:00", "stderr": "[av1_qsv] Error initializing the encoder", "rc": 218})
        self.assertEqual(self.store.conn.execute("SELECT state FROM v_cell_state WHERE cell_key = 'c-new'").fetchone()[0], "failed")

    def test_record_kind_must_match_run_stage(self):
        self.encode_run()
        self.refused("r2", {"kind": "score", "cell_key": "c-new", "recipe": "S1", "scores": []},
                     "REFUSING: a score record under an encode run -- score records come from score runs")

    def test_record_for_an_unplanned_cell_is_refused(self):
        self.encode_run()
        self.refused("r2", dict(ENCODE, cell_key="c-a24-tng"), "REFUSING: cell 'c-a24-tng' is not planned in run 'r2' -- a record names a cell its run planned")

    def test_reposting_an_identical_record_is_a_noop(self):
        self.encode_run()
        self.post("r2", ENCODE)
        self.post("r2", ENCODE)
        self.assertEqual(len(self.rows("encode", cell_key="c-new")), 1)

    def test_reposting_a_differing_record_is_refused(self):
        self.encode_run()
        self.post("r2", ENCODE)
        self.refused("r2", dict(ENCODE, bytes=1), "REFUSING: cell 'c-new' already has an encode record that differs -- a record is never overwritten; abandon the run and re-plan")

    # ---- score
    SCORE = {"kind": "score", "cell_key": "c-new", "recipe": "S1", "kept": False,
             "scores": [{"metric": "ssimulacra2", "statistic": "mean", "value": 83.0}, {"metric": "vmaf", "statistic": "mean", "value": 96.0}],
             "steps": [{"scoring_step": "libvmaf", "seconds": 51.4, "cores_busy": 11.0, "gpu_mean": 15.9, "gpu_max": 22.0}]}

    def score_run(self):
        self.encode_run()
        self.post("r2", ENCODE)
        self.plan("r2-score", "score", host="media-01-score", pins=SCORER, unit=None, cc=None, search=None, windows=(), parent_run_id="r2",
                  scorer_build="FFVship 1.3 + 8.1.2-0b0ea2d", ffvship_version="1.3", metric_backend="libvmaf_cuda")

    def test_score_record_reaches_score_and_step_trace_under_the_score_run(self):
        self.score_run()
        self.post("r2-score", self.SCORE)
        scores = self.rows("score", cell_key="c-new")
        self.assertEqual({(s["run_id"], s["metric"], s["scorer_build"]) for s in scores}, {("r2-score", "ssimulacra2", "FFVship 1.3 + 8.1.2-0b0ea2d"), ("r2-score", "vmaf", "FFVship 1.3 + 8.1.2-0b0ea2d")})
        self.assertEqual(self.rows("step_trace", cell_key="c-new")[0]["run_id"], "r2-score")
        self.assertEqual(self.store.check(), {})

    def test_score_height_comes_from_the_search(self):
        self.score_run()
        self.post("r2-score", self.SCORE)
        self.assertEqual({s["height"] for s in self.rows("score", cell_key="c-new")}, {1548})

    def test_score_record_flips_kept(self):
        self.score_run()
        self.post("r2-score", self.SCORE)
        self.assertEqual(self.rows("encode", cell_key="c-new")[0]["kept"], 0)

    def test_score_run_without_a_search_is_refused(self):
        self.plan("r2-screen", "screen", search=None, cells=(store.CellPlan("s2-a", "tng", "reference", (("qsv.preset", "1", "identity"), ("qsv.q", "30", "identity"))),))
        self.post("r2-screen", dict(ENCODE, cell_key="s2-a", kept=False))
        self.plan("r2-screen-score", "score", host="media-01-score", pins=SCORER, unit=None, cc=None, search=None, windows=(), parent_run_id="r2-screen",
                  scorer_build="b", ffvship_version="1.3", metric_backend="libvmaf_cuda")
        self.refused("r2-screen-score", dict(self.SCORE, cell_key="s2-a"),
                     "REFUSING: score run 'r2-screen-score' has no search to take the height from -- score runs are planned from a search's encoding run; the height is the search's")

    def test_score_record_reposted_identically_is_a_noop_and_differing_is_refused(self):
        self.score_run()
        self.post("r2-score", self.SCORE)
        self.post("r2-score", self.SCORE)
        self.assertEqual(len(self.rows("score", cell_key="c-new")), 2)
        self.refused("r2-score", dict(self.SCORE, scores=[{"metric": "vmaf", "statistic": "mean", "value": 1.0}]),
                     "REFUSING: cell 'c-new' already has a score record that differs -- a record is never overwritten; abandon the run and re-plan")

    # ---- timing
    def test_timing_record_reaches_timing(self):
        self.plan("r2-time", "time", cells=(store.CellPlan("t-new", "tng", "source", ARM_A),))
        self.post("r2-time", dict(ENCODE, cell_key="t-new", kept=False),
                  {"kind": "timing", "cell_key": "t-new", "samples": [
                      {"workers": 1, "repeat_index": 0, "fps": 640.0, "wall_s": 2.2, "decode_path": "hardware", "is_warmup": True, "noise_floor_pct": 2.8, "frames": 1439},
                      {"workers": 1, "repeat_index": 1, "fps": 690.0, "wall_s": 2.1, "decode_path": "hardware", "is_warmup": False, "noise_floor_pct": 2.8, "frames": 1439}]})
        self.assertEqual([(r["repeat_index"], r["is_warmup"], r["leg"]) for r in self.rows("timing", cell_key="t-new")], [(0, 1, "full"), (1, 0, "full")])
        self.assertEqual(self.store.check(), {})

    # ---- the screen's verdicts
    def screen_run(self):
        self.plan("r2-screen", "screen", search=None, windows=("parks",), cells=(
            store.CellPlan("s2-a", "parks", "reference", (("qsv.preset", "1", "identity"), ("qsv.q", "30", "identity"))),
            store.CellPlan("s2-b", "parks", "reference", (("qsv.preset", "4", "identity"), ("qsv.q", "30", "identity")))))
        self.post("r2-screen", dict(ENCODE, cell_key="s2-a", kept=False), dict(ENCODE, cell_key="s2-b", kept=False))

    def test_screen_verdict_record_reaches_setting_verdict_with_its_cells(self):
        self.screen_run()
        self.post("r2-screen", {"kind": "screen_verdict", "setting_id": "qsv.preset", "verdict": "HONOURED", "magnitude_pct": 5.0,
                                "noise_floor_pct": 2.8, "cells": ["s2-a", "s2-b"]})
        (v,) = self.rows("setting_verdict", setting_id="qsv.preset", window_id="parks")
        self.assertEqual((v["encoder_unit_id"], v["verdict"]), (B580, "HONOURED"))
        self.assertEqual(len(self.rows("setting_verdict_cell", verdict_id=v["verdict_id"])), 2)
        self.assertEqual(self.store.check(), {})

    def test_screen_verdict_cells_must_share_a_window(self):
        self.plan("r2-screen-b", "screen", search=None, windows=("tng", "parks"), cells=(
            store.CellPlan("s2-c", "tng", "reference", (("qsv.preset", "1", "identity"), ("qsv.q", "30", "identity"))),
            store.CellPlan("s2-d", "parks", "reference", (("qsv.preset", "1", "identity"), ("qsv.q", "30", "identity")))))
        self.post("r2-screen-b", dict(ENCODE, cell_key="s2-c", kept=False), dict(ENCODE, cell_key="s2-d", kept=False))
        self.refused("r2-screen-b", {"kind": "screen_verdict", "setting_id": "qsv.preset", "verdict": "HONOURED", "cells": ["s2-c", "s2-d"]},
                     "REFUSING: a screen verdict over cells on more than one window -- a verdict is per window; post one per window")

    def test_admissibility_verdict_record_reaches_its_table_with_its_cells(self):
        self.screen_run()
        self.post("r2-screen", {"kind": "admissibility_verdict", "setting_id": "qsv.q", "test": "obeys_rate", "verdict": "ADMISSIBLE", "cells": ["s2-a"]})
        (v,) = self.rows("admissibility_verdict", test="obeys_rate")
        self.assertEqual((v["encoder_unit_id"], v["window_id"]), (B580, "parks"))
        self.assertEqual(self.rows("admissibility_verdict_cell", admissibility_id=v["admissibility_id"])[0]["cell_key"], "s2-a")
        self.assertEqual(self.store.check(), {})

    # ---- the sample's rows: title, reference set, cut, cut check
    TITLE = {"kind": "title", "title_id": "newfilm", "path": "/mnt/storage/Movies/New (2026)/New.mkv", "library": "Movies", "width": 1920, "height": 1080,
             "dynamic_range": "sdr", "video_codec": "h264", "field_order": "progressive", "fps": 23.976, "bit_depth": 8, "bitrate_kbps": 20000,
             "bpp": 0.4, "source_type": "remux", "scanned_at": "2026-09-05"}

    def test_title_record_reaches_title_and_a_rescan_updates_it(self):
        self.plan("inv2", "inventory", unit=None, cc=None, search=None, windows=())
        self.post("inv2", self.TITLE)
        self.post("inv2", dict(self.TITLE, bitrate_kbps=21000, scanned_at="2026-09-06"))
        (row,) = self.rows("title", title_id="newfilm")
        self.assertEqual((row["bitrate_kbps"], row["scanned_at"]), (21000.0, "2026-09-06"))

    def materialise_run(self):
        self.plan("mat2", "materialise", cc=None, search=None, windows=("tng",))
        self.post("mat2", {"kind": "reference_set", "reference_set_id": "stage-1080p-b", "geometry": "1920x1080", "pix_fmt": "p010le",
                           "built_with": "8.1.2-Jellyfin 0b0ea2d", "built_at": "2026-09-05"},
                  {"kind": "cut", "cut_id": "tng.ref.b", "reference_set_id": "stage-1080p-b", "window_id": "tng", "cut_kind": "reference",
                   "chain_lane": "m4-ipad-le1080p-sdr", "chain_host": "media-01", "chain_unit": B580, "content_sha": "sha-tng-ref-b", "bytes": 2000000000, "frames": 1439})

    def test_reference_set_and_cut_records_reach_their_tables(self):
        self.materialise_run()
        self.assertEqual(self.rows("reference_set", reference_set_id="stage-1080p-b")[0]["built_on"], "media-01")
        self.assertEqual(self.rows("cut", cut_id="tng.ref.b")[0]["kind"], "reference")
        self.assertEqual(self.store.check(), {})

    def test_a_cut_differing_by_content_is_refused(self):
        self.materialise_run()
        self.refused("mat2", {"kind": "cut", "cut_id": "tng.ref.b", "reference_set_id": "stage-1080p-b", "window_id": "tng", "cut_kind": "reference",
                              "chain_lane": "m4-ipad-le1080p-sdr", "chain_host": "media-01", "chain_unit": B580, "content_sha": "other", "bytes": 1, "frames": 1439},
                     "REFUSING: cut 'tng.ref.b' already has a record that differs -- a record is never overwritten; a cut is re-materialised only by content hash")

    def test_cut_check_record_reaches_cut_check(self):
        self.materialise_run()
        self.plan("ver2", "verify", unit=None, cc=None, search=None, windows=())
        self.post("ver2", {"kind": "cut_check", "cut_id": "tng.ref.b", "check_name": "content", "result": "pass", "checked_at": "2026-09-05"})
        self.assertEqual(self.rows("cut_check", cut_id="tng.ref.b")[0]["result"], "pass")


if __name__ == "__main__":
    unittest.main()
