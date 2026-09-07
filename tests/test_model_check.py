#!/usr/bin/env python3
"""sweep/model_check.py: the schema loads, the fixture is clean, every check fires on its negative
case, every DDL refusal refuses, and DATA-MODEL.md's generated regions are current.

    python3 -m unittest tests.test_model_check
"""
import sqlite3
import unittest
from pathlib import Path

from sweep import model_check as mc


class SchemaLoads(unittest.TestCase):
    def test_loads_and_every_table_is_tagged(self):
        conn = mc.load_schema()
        self.addCleanup(conn.close)
        tags, checks = mc.parse_tags()
        self.assertEqual(mc.check_tags(conn, tags), [])
        self.assertEqual(set(mc.db_tables(conn)), set(tags))

    def test_every_check_view_has_a_description(self):
        conn = mc.load_schema()
        self.addCleanup(conn.close)
        _, checks = mc.parse_tags()
        for v in mc.db_views(conn, "x_"):
            self.assertTrue(checks.get(v, {}).get("check"), f"{v} has no -- @check line")

    def test_every_check_has_a_fix(self):
        # every refusal is `REFUSING: <what> -- <fix>`; the fix is authored beside the check, never invented by the store
        conn = mc.load_schema()
        self.addCleanup(conn.close)
        _, checks = mc.parse_tags()
        for v in mc.db_views(conn, "x_"):
            entry = checks.get(v)
            self.assertIsInstance(entry, dict, f"{v}: parse_tags gives no check/fix pair")
            self.assertTrue(entry.get("fix"), f"{v} has no -- @fix line")
        for k, entry in mc.SCRIPT_CHECKS.items():
            self.assertIsInstance(entry, tuple, f"{k}: SCRIPT_CHECKS gives no (refuses, fix) pair")
            self.assertTrue(entry[1], f"{k} has no fix")

    def test_every_logic_check_constraint_is_named(self):
        # the store maps a failed CHECK to its fix by name; an enum CHECK stays unnamed (its message is the expression)
        unnamed = [expr for name, expr in mc.check_constraints() if name is None and not mc.is_enum_check(expr)]
        self.assertEqual(unnamed, [], "logic CHECKs without a CONSTRAINT name")

    def test_every_table_has_one_writer_and_one_class(self):
        tags, _ = mc.parse_tags()
        for t, tg in tags.items():
            self.assertIn(tg["class"], ("FILE", "ROW"), t)
            self.assertTrue(tg["writer"], t)
            self.assertIn(tg["group"], mc.GROUPS, t)


class FixtureIsClean(unittest.TestCase):
    def test_no_check_fires(self):
        conn = mc.load_schema()
        self.addCleanup(conn.close)
        tags, _ = mc.parse_tags()
        mc.load_fixture(conn)
        results = mc.run_checks(conn, tags)
        bad = {k: v for k, v in results.items() if v}
        self.assertEqual(bad, {})

    def test_preconditions_are_scoped_to_their_stage(self):
        # five checks are preconditions of a later stage, not store invariants: built verb by verb, the store passes
        # through these states, and each must be clean
        tags, _ = mc.parse_tags()
        # a lane with content before the library is scanned: no title at all is not an empty population
        conn = mc.load_schema()
        self.addCleanup(conn.close)
        conn.executescript("INSERT INTO lane VALUES ('l','av1','incumbent',NULL,NULL,'sdr','native','sdr','n/a','a','s',NULL,1548,'never',NULL,1,NULL)")
        self.assertEqual(mc.run_checks(conn, tags)["x_has_content_but_empty"], [])
        # a measured constant before any lane in its scope ships: calibrate comes later than add-constant
        conn.executescript("INSERT INTO constant VALUES ('HEADROOM',NULL,'fraction','measured',NULL,NULL,NULL,NULL); "
                           "INSERT INTO constant_scope VALUES ('HEADROOM','l')")
        self.assertEqual(mc.run_checks(conn, tags)["x_measured_constant_never_calibrated"], [])
        # the ladder and the incumbent's bar are complete only once the search names its shipping arm
        conn = mc.load_schema()
        self.addCleanup(conn.close)
        mc.load_fixture(conn)
        conn.executescript("UPDATE search SET shipping_arm_id = NULL; "
                           "UPDATE cell_setting SET value = '23' WHERE cell_key = 'c-a24-tng' AND setting_id = 'qsv.q'; "
                           "DELETE FROM score WHERE cell_key = 'c-i30-parks'; UPDATE encode SET kept = 1 WHERE cell_key = 'c-i30-parks'")
        results = mc.run_checks(conn, tags)
        self.assertEqual(results["shipping_arm_ladder_complete"], [])
        self.assertEqual(results["incumbent_arm_scored"], [])
        # a host with completed runs must be blockable; the refusal is at the moment of use
        conn = mc.load_schema()
        self.addCleanup(conn.close)
        mc.load_fixture(conn)
        conn.executescript("UPDATE host SET blocked = 'the fix' WHERE host = 'media-01'")
        self.assertEqual(mc.run_checks(conn, tags)["x_run_on_a_blocked_host"], [])

    def test_unit_reading_is_derived_per_window(self):
        conn = mc.load_schema()
        self.addCleanup(conn.close)
        mc.load_fixture(conn)
        rows = {r[1]: r[7] for r in conn.execute("SELECT * FROM v_setting_unit_reading")}
        self.assertEqual(rows["qsv.b_strategy"], "HONOURED")        # one window is enough
        self.assertEqual(rows["qsv.adaptive_b"], "INCOMPLETE 1 of 6")  # one window cannot exclude

    def test_run_progress_of_a_score_run_reads_its_parents_cells(self):
        conn = mc.load_schema()
        self.addCleanup(conn.close)
        mc.load_fixture(conn)
        rows = {r[0]: r for r in conn.execute(
            "SELECT run_id, planned_total, still_planned, encoded, scored, failed FROM v_run_progress")}
        self.assertEqual(rows["b580-qsv-av1"][1:], (89, 0, 0, 88, 1))          # the encode run: its own cells, one failed
        self.assertEqual(rows["b580-qsv-av1-score"][1:], (89, 0, 0, 89, 1))    # the score run: the parent's cells, scored by THIS run

    def test_representation_is_counted(self):
        conn = mc.load_schema()
        self.addCleanup(conn.close)
        mc.load_fixture(conn)
        (members, represented), = conn.execute(
            "SELECT members, represented FROM v_class_lane_representation").fetchall()
        self.assertEqual((members, represented), (6, 6))


class EveryCheckFires(unittest.TestCase):
    def test_each_negative_case_is_caught(self):
        tags, _ = mc.parse_tags()
        for name, expected, fired, _others in mc.run_mutations(tags):
            with self.subTest(case=name):
                self.assertTrue(fired, f"{name}: {expected} did not fire")

    def test_every_check_has_a_negative_case(self):
        conn = mc.load_schema()
        self.addCleanup(conn.close)
        covered = {expected for _, _, expected in mc.MUTATIONS}
        for v in mc.db_views(conn, "x_"):
            self.assertIn(v, covered, f"{v} has no negative case -- a check that never fires is not a check")
        for k in mc.SCRIPT_CHECKS:
            if k != "tags_complete":
                self.assertIn(k, covered, k)

    def test_ddl_refusals_refuse(self):
        for name, refused in mc.run_ddl_refusals():
            with self.subTest(case=name):
                self.assertTrue(refused, f"{name}: the DDL allowed it")

    def test_a_negative_case_that_changes_nothing_is_not_caught(self):
        # the harness must not report CAUGHT for a no-op: the RED-ON-CLEAN trap
        tags, _ = mc.parse_tags()
        conn = mc.load_schema()
        self.addCleanup(conn.close)
        mc.load_fixture(conn)
        conn.executescript("UPDATE window SET notes = notes WHERE window_id = 'tng'")
        self.assertEqual({k: v for k, v in mc.run_checks(conn, tags).items() if v}, {})


class DocIsRendered(unittest.TestCase):
    def test_generated_regions_are_current(self):
        conn = mc.load_schema()
        self.addCleanup(conn.close)
        tags, checks = mc.parse_tags()
        regions = mc.render_regions(conn, tags, checks)
        _, missing, stale = mc.apply_regions(mc.DOC.read_text(), regions)
        self.assertEqual(missing, [], "DATA-MODEL.md lacks a marker pair")
        self.assertEqual(stale, [], "run: python3 sweep/model_check.py --render")


if __name__ == "__main__":
    unittest.main()
