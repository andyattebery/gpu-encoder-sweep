#!/usr/bin/env python3
"""sweep/hub/refusals.py and sweep/hub/store.py: every write is one checked transaction, and every refusal is
`REFUSING: <what> -- <fix>` composed from the schema.

    python3 -m unittest tests.test_hub_store
"""
import pathlib
import re
import sqlite3
import tempfile
import unittest

from sweep import model_check as mc
from sweep.hub import refusals, store
from sweep.hub.refusals import Refusal
from tests.hub_helpers import fixture_store

FORM = r"^REFUSING: .+ -- .+$"


def integrity_error(conn, sql):
    try:
        conn.executescript(sql)
    except sqlite3.IntegrityError as e:
        return e
    raise AssertionError(f"the DDL allowed: {sql}")


class RefusalsFromSqlite(unittest.TestCase):
    def setUp(self):
        self.store = fixture_store()
        self.conn = self.store.conn

    def test_named_check_constraint_maps_to_its_fix(self):
        err = integrity_error(self.conn, "UPDATE shipped SET reason = NULL WHERE shipped_id = 9")
        r = refusals.integrity_refusal(err)
        self.assertRegex(str(r), FORM)
        self.assertEqual(r.fix, refusals.CONSTRAINT_FIXES["shipped_policy_has_reason"][1])
        self.assertIn("policy", r.what)

    def test_enum_check_reports_column_and_values(self):
        err = integrity_error(self.conn, "UPDATE host SET os = 'plan9' WHERE host = 'nas-01'")
        r = refusals.integrity_refusal(err)
        self.assertRegex(str(r), FORM)
        self.assertEqual(r.what, "os must be one of linux, windows")

    def test_not_null_maps_to_the_column(self):
        err = integrity_error(self.conn, "INSERT INTO host (host, ssh_host, os, work_root, ffmpeg) VALUES ('h', 'h', 'linux', '/w', '/f')")
        r = refusals.integrity_refusal(err)
        self.assertEqual(r.what, "host.machine is missing")
        self.assertEqual(r.fix, refusals.NOT_NULL_FIXES[("host", "machine")])
        err = integrity_error(self.conn, "INSERT INTO run_event (run_id, at, state) VALUES ('b580-qsv-av1', '2026-09-03T03:00', 'running')")
        self.assertEqual(refusals.integrity_refusal(err).fix, "give --by")   # the default: the column's flag

    def test_unique_maps_to_the_table_fix(self):
        err = integrity_error(self.conn, "INSERT INTO ladder VALUES ('av1-b', 'av1')")
        r = refusals.integrity_refusal(err)
        self.assertEqual(r.what, "ladder already has a row with that codec")
        self.assertEqual(r.fix, refusals.UNIQUE_FIXES["ladder"])
        err = integrity_error(self.conn, "INSERT INTO canonical_concept VALUES ('preset', 'again')")
        self.assertIn("never overwritten", refusals.integrity_refusal(err).fix)   # the default for a FILE table

    def test_foreign_key_error_without_precheck_still_refuses(self):
        err = integrity_error(self.conn, "INSERT INTO constant_scope VALUES ('CEILING', 'no-such-lane')")
        r = refusals.integrity_refusal(err)
        self.assertRegex(str(r), FORM)
        self.assertIn("does not exist", r.what)

    def test_wrong_type_reports_the_column(self):
        err = integrity_error(self.conn, "UPDATE ladder_rung SET rung = 'high' WHERE ladder_id = 'av1' AND rung = 60")
        r = refusals.integrity_refusal(err)
        self.assertEqual(r.what, "ladder_rung.rung must be an INTEGER, not TEXT")

    def test_every_named_constraint_has_a_fix(self):
        self.assertEqual(set(refusals.named_constraints()), set(refusals.CONSTRAINT_FIXES))
        for name, (what, fix) in refusals.CONSTRAINT_FIXES.items():
            self.assertRegex(mc.refusing(what, fix), FORM, name)

    def test_check_refusal_names_the_check_a_row_and_the_others(self):
        r = refusals.check_refusal("x_class_serves_no_lane", "a class that serves no lane", [("other",)],
                                   "define-class with at least one lane", ["x_measured_on_a_class_not_for_the_lane"])
        self.assertEqual(str(r), "REFUSING: x_class_serves_no_lane: a class that serves no lane, e.g. ('other',) "
                                 "(x_measured_on_a_class_not_for_the_lane also fires) -- define-class with at least one lane")


class StoreOpens(unittest.TestCase):
    def test_opens_memory_store_with_schema_and_foreign_keys(self):
        s = store.Store()
        self.addCleanup(s.close)
        self.assertEqual(set(mc.db_tables(s.conn)), set(s.tags))
        self.assertEqual(s.conn.execute("PRAGMA foreign_keys").fetchone()[0], 1)
        self.assertEqual(s.conn.execute("PRAGMA user_version").fetchone()[0], s.version)
        self.assertEqual(s.check(), {})

    def test_file_store_uses_wal_and_reopens(self):
        path = str(pathlib.Path(tempfile.mkdtemp()) / "hub.sqlite")
        s = store.Store(path)
        self.assertEqual(s.conn.execute("PRAGMA journal_mode").fetchone()[0], "wal")
        with s.transaction() as conn:
            store.insert(conn, "canonical_concept", {"canonical_id": "c", "description": "d"})
        s.close()
        s2 = store.Store(path)
        self.addCleanup(s2.close)
        self.assertEqual([r["canonical_id"] for r in s2.rows("canonical_concept")], ["c"])

    def test_reopening_a_store_from_another_schema_is_refused(self):
        path = str(pathlib.Path(tempfile.mkdtemp()) / "hub.sqlite")
        s = store.Store(path)
        s.conn.execute("PRAGMA user_version = 1")
        s.close()
        with self.assertRaises(Refusal) as cm:
            store.Store(path)
        self.assertRegex(str(cm.exception), FORM)
        self.assertIn("another schema", cm.exception.what)


class Transactions(unittest.TestCase):
    def setUp(self):
        self.store = fixture_store()
        self.addCleanup(self.store.close)

    def test_transaction_commits_when_no_check_fires(self):
        with self.store.transaction() as conn:
            store.insert(conn, "canonical_concept", {"canonical_id": "lookahead", "description": "frames of lookahead"})
        self.assertIn("lookahead", [r["canonical_id"] for r in self.store.rows("canonical_concept")])
        self.assertEqual(self.store.check(), {})

    def test_transaction_rolls_back_and_refuses_when_a_check_fires(self):
        with self.assertRaises(Refusal) as cm:
            with self.store.transaction() as conn:
                conn.execute("DELETE FROM scorer")
        self.assertRegex(str(cm.exception), FORM)
        self.assertTrue(cm.exception.what.startswith("x_score_run_host_without_scorer: "))
        self.assertEqual(cm.exception.fix, self.store.checks["x_score_run_host_without_scorer"]["fix"])
        self.assertEqual(len(self.store.rows("scorer")), 1)           # rolled back
        self.assertEqual(self.store.check(), {})

    def test_transaction_rolls_back_and_refuses_on_an_integrity_error(self):
        with self.assertRaises(Refusal) as cm:
            with self.store.transaction() as conn:
                conn.execute("UPDATE shipped SET reason = NULL WHERE shipped_id = 9")
        self.assertEqual(cm.exception.fix, refusals.CONSTRAINT_FIXES["shipped_policy_has_reason"][1])
        self.assertIsNotNone(self.store.rows("shipped")[8]["reason"])

    def test_a_refusal_inside_the_body_rolls_back(self):
        with self.assertRaises(Refusal):
            with self.store.transaction() as conn:
                store.insert(conn, "canonical_concept", {"canonical_id": "x", "description": "d"})
                store.require(conn, "lane", lane="no-such-lane")
        self.assertNotIn("x", [r["canonical_id"] for r in self.store.rows("canonical_concept")])

    def test_check_reports_only_the_firing_checks(self):
        self.store.conn.execute("DELETE FROM scorer")
        firing = self.store.check()
        self.assertEqual(list(firing), ["x_score_run_host_without_scorer"])
        self.assertEqual(set(firing["x_score_run_host_without_scorer"]), {("b580-qsv-av1-score", "media-01-score"), ("b580-viewing-score", "media-01-score")})

    def test_require_refuses_a_missing_reference_by_name(self):
        with self.store.reading() as conn:
            store.require(conn, "lane", lane="m4-ipad-le1080p-sdr")
            store.require(conn, "host_unit", host="media-01", encoder_unit_id="intel-b580-ihd26.2.2-qsv-av1")
            with self.assertRaises(Refusal) as cm:
                store.require(conn, "lane", lane="no-such-lane")
        self.assertEqual(str(cm.exception), "REFUSING: lane lane='no-such-lane' does not exist -- add it first with add-lane")

    def test_rows_are_ordered_by_the_primary_key(self):
        self.assertEqual([r["shipped_id"] for r in self.store.rows("shipped")], list(range(1, 10)))
        chains = [(r["lane"], r["host"], r["encoder_unit_id"]) for r in self.store.rows("chain")]
        self.assertEqual(chains, sorted(chains))
        self.assertEqual(list(self.store.rows("host")[0]), [c["name"] for c in mc.columns(self.store.conn, "host")])

    def test_insert_returning_id_gives_the_new_surrogate(self):
        with self.store.transaction() as conn:
            vid = store.insert_returning_id(conn, "viewing_verdict", {
                "kind": "pair", "window_id": "tng", "cell_a": "g-a", "cell_b": "g-b", "device": "iPad M4 13in",
                "viewer": "andy", "verdict": "same", "viewed_at": "2026-09-05"}, "viewing_id")
        self.assertEqual(vid, 3)


class PlansEventsAndCalibration(unittest.TestCase):
    def setUp(self):
        self.store = fixture_store()
        self.addCleanup(self.store.close)

    def test_insert_cut_check_classified_needs_a_reason(self):
        with self.assertRaises(Refusal) as cm:
            with self.store.transaction() as conn:
                store.insert_cut_check(conn, "tng.ref", "dv_rpu", "classified", None, "2026-08-27")
        self.assertRegex(str(cm.exception), FORM)
        with self.store.transaction() as conn:
            store.insert_cut_check(conn, "tng.ref", "dv_rpu", "classified", "no RPU: the source is HDR10", "2026-08-27")
        self.assertEqual(len([r for r in self.store.rows("cut_check") if r["cut_id"] == "tng.ref"]), 2)

    def encode_plan(self):
        return store.RunPlan(run_id="r2", stage="encode", host="media-01", node_label="media-01-b580",
                             artifact="node-encode:0.0.2.dev0+g0", ffmpeg_build="8.1.2-Jellyfin", ffmpeg_sha="0b0ea2d",
                             harness_version="g0", planned_at="2026-09-05T09:00",
                             encoder_unit_id="intel-b580-ihd26.2.2-qsv-av1", content_class_id="native-1080p-sdr",
                             search_id="b580-qsv-av1", windows=("tng",),
                             cells=(store.CellPlan("c-new", "tng", "reference",
                                                   (("qsv.q", "24", "identity"), ("qsv.preset", "4", "identity"),
                                                    ("qsv.b_strategy", "0", "identity"))),))

    def test_plan_run_writes_run_windows_cells_settings_and_planned_event(self):
        with self.store.transaction() as conn:
            store.plan_run(conn, self.encode_plan())
        run = [r for r in self.store.rows("run") if r["run_id"] == "r2"][0]
        self.assertEqual((run["state"], run["started_at"], run["parent_run_id"]), ("planned", "2026-09-05T09:00", None))
        self.assertEqual([r["window_id"] for r in self.store.rows("run_window") if r["run_id"] == "r2"], ["tng"])
        self.assertEqual([r["cell_key"] for r in self.store.rows("cell") if r["run_id"] == "r2"], ["c-new"])
        self.assertEqual(len([r for r in self.store.rows("cell_setting") if r["cell_key"] == "c-new"]), 3)
        events = [r for r in self.store.rows("run_event") if r["run_id"] == "r2"]
        self.assertEqual([(e["at"], e["state"], e["by"]) for e in events], [("2026-09-05T09:00", "planned", "hub")])
        self.assertEqual(self.store.check(), {})

    def test_plan_run_of_a_score_run_copies_the_parent(self):
        plan = store.RunPlan(run_id="r-score", stage="score", host="media-01-score", node_label="media-01-score",
                             artifact="node-score:0.0.2.dev0+g0", ffmpeg_build="8.1.2-Jellyfin", ffmpeg_sha="0b0ea2d",
                             harness_version="g0", planned_at="2026-09-05T09:00", parent_run_id="b580-qsv-av1",
                             scorer_build="FFVship 1.3 + 8.1.2-0b0ea2d", ffvship_version="1.3", metric_backend="libvmaf_cuda")
        with self.store.transaction() as conn:
            store.plan_run(conn, plan)
        run = [r for r in self.store.rows("run") if r["run_id"] == "r-score"][0]
        self.assertEqual((run["encoder_unit_id"], run["content_class_id"], run["search_id"]),
                         ("intel-b580-ihd26.2.2-qsv-av1", "native-1080p-sdr", "b580-qsv-av1"))
        self.assertEqual(sorted(r["window_id"] for r in self.store.rows("run_window") if r["run_id"] == "r-score"),
                         sorted(r["window_id"] for r in self.store.rows("run_window") if r["run_id"] == "b580-qsv-av1"))
        self.assertEqual(self.store.check(), {})

    def test_post_event_updates_state_and_complete_sets_verified_at(self):
        with self.store.transaction() as conn:
            store.plan_run(conn, self.encode_plan())
            store.post_event(conn, "r2", "2026-09-05T10:00", "launched", "claimed by media-01")
            store.post_event(conn, "r2", "2026-09-05T10:00:30", "running", "first cell started", by="agent")
            store.insert(conn, "encode", {"cell_key": "c-new", "bytes": 60000000, "bitrate_kbps": 8000.0, "frames": 1439,
                                          "duration_s": 60.0, "decode_path": "hardware", "kept": 1})
            store.post_event(conn, "r2", "2026-09-05T11:00", "complete", "count and heights verified against the plan")
        run = [r for r in self.store.rows("run") if r["run_id"] == "r2"][0]
        self.assertEqual(run["state"], "complete")
        self.assertEqual((run["started_at"], run["finished_at"], run["fetched_at"], run["verified_at"]),
                         ("2026-09-05T10:00", "2026-09-05T11:00", "2026-09-05T11:00", "2026-09-05T11:00"))
        self.assertEqual([e["by"] for e in self.store.rows("run_event") if e["run_id"] == "r2"], ["hub", "hub", "agent", "hub"])
        self.assertEqual(self.store.check(), {})

    def test_post_event_on_an_unknown_run_is_refused(self):
        with self.assertRaises(Refusal) as cm:
            with self.store.transaction() as conn:
                store.post_event(conn, "nope", "2026-09-05T10:00", "launched")
        self.assertIn("run run_id='nope' does not exist", str(cm.exception))

    def test_calibrate_writes_constant_value(self):
        plan = store.RunPlan(run_id="m4-calibrate-2", stage="calibrate", host="media-01", node_label="media-01",
                             artifact="node-encode:0.0.2.dev0+g0", ffmpeg_build="8.1.2-Jellyfin", ffmpeg_sha="0b0ea2d",
                             harness_version="g0", planned_at="2026-09-05T09:00", encoder_unit_id="nvidia-a4000-595-nvenc-hevc")
        with self.store.transaction() as conn:
            store.plan_run(conn, plan)
            store.calibrate(conn, "HEADROOM", "m4-calibrate-2", 0.95, "2026-09-05")
        values = [r for r in self.store.rows("constant_value") if r["name"] == "HEADROOM"]
        self.assertEqual([(v["run_id"], v["value"]) for v in values], [("m4-calibrate", 0.98), ("m4-calibrate-2", 0.95)])
        self.assertEqual(self.store.conn.execute("SELECT value FROM v_constant_current WHERE name = 'HEADROOM'").fetchone()[0], 0.95)

    def test_calibrate_refuses_a_constant_that_is_not_measured(self):
        with self.assertRaises(Refusal) as cm:
            with self.store.transaction() as conn:
                store.calibrate(conn, "CEILING", "m4-calibrate", 20.0, "2026-09-05")
        self.assertEqual(str(cm.exception), "REFUSING: CEILING is derived, not measured -- calibrate writes measured constants only; a derived or policy value is authored with add-constant")


if __name__ == "__main__":
    unittest.main()
