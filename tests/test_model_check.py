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
            self.assertTrue(checks.get(v), f"{v} has no -- @check line")

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

    def test_unit_reading_is_derived_per_window(self):
        conn = mc.load_schema()
        self.addCleanup(conn.close)
        mc.load_fixture(conn)
        rows = {r[1]: r[7] for r in conn.execute("SELECT * FROM v_setting_unit_reading")}
        self.assertEqual(rows["qsv.b_strategy"], "HONOURED")        # one window is enough
        self.assertEqual(rows["qsv.adaptive_b"], "INCOMPLETE 1 of 6")  # one window cannot exclude

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
