#!/usr/bin/env python3
"""sweep/hub/export.py: the record as files, deterministic -- the same store renders the same bytes, a changed row
changes exactly one file, a stale run directory goes, and every table has a home.

    python3 -m unittest tests.test_hub_export
"""
import hashlib
import json
import pathlib
import tempfile
import unittest

from sweep import model_check as mc
from sweep.hub import export
from tests.hub_helpers import client_for, fixture_store


def tree(root):
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(root.rglob("*")) if p.is_file()}


class Export(unittest.TestCase):
    def setUp(self):
        self.store = fixture_store()
        self.addCleanup(self.store.close)
        self.client = client_for(self.store)
        self.openapi = self.client.app.openapi()

    def render(self):
        with self.store.reading() as conn:
            return export.render(conn, self.openapi)

    def test_every_table_has_a_home(self):
        self.assertEqual(set(export.EXPORT_HOMES), set(mc.db_tables(self.store.conn)))

    def test_render_twice_is_equal(self):
        self.assertEqual(self.render(), self.render())

    def test_render_lays_the_record_out_by_home(self):
        files = self.render()
        self.assertIn("authored/host.json", files)
        self.assertIn("sample/cut.json", files)
        self.assertIn("agents/host_identity.json", files)
        self.assertIn("agents/published.json", files)
        self.assertIn("runs/b580-qsv-av1/plan.json", files)
        self.assertIn("runs/b580-qsv-av1/events.jsonl", files)
        self.assertIn("runs/b580-qsv-av1/records/encode.json", files)
        self.assertIn("runs/b580-qsv-av1-score/records/score.json", files)
        self.assertIn("runs/b580-qsv-av1-screen/records/setting_verdict.json", files)
        self.assertIn("constants.json", files)
        self.assertIn("openapi.json", files)
        plan = json.loads(files["runs/b580-qsv-av1/plan.json"])
        self.assertEqual(plan["run"]["stage"], "encode")
        self.assertEqual(len(plan["cells"]), 89)
        self.assertEqual(plan["cells"][0]["settings"][0]["setting_id"], "qsv.adaptive_b")     # keys and rows sorted
        verdict = json.loads(files["runs/b580-qsv-av1-screen/records/setting_verdict.json"])[0]
        self.assertEqual(verdict["cells"], ["s-bs0", "s-bs1"])
        self.assertEqual(len(files["runs/b580-qsv-av1/events.jsonl"].splitlines()), 3)
        self.assertTrue(files["authored/host.json"].endswith("\n"))

    def test_a_changed_row_changes_exactly_one_file(self):
        before = self.render()
        self.store.conn.execute("UPDATE host SET notes = 'a note' WHERE host = 'nas-01'")
        after = self.render()
        self.assertEqual([p for p in before if before[p] != after[p]], ["authored/host.json"])
        self.assertEqual(set(before), set(after))

    def test_write_twice_is_byte_identical(self):
        into = pathlib.Path(tempfile.mkdtemp())
        written = export.write(self.render(), into)
        self.assertEqual(sorted(written), sorted(self.render()))
        first = tree(into)
        export.write(self.render(), into)
        self.assertEqual(tree(into), first)

    def test_stale_run_directory_is_removed_and_foreign_files_stay(self):
        into = pathlib.Path(tempfile.mkdtemp())
        export.write(self.render(), into)
        stale = into / "runs" / "old-run" / "plan.json"
        stale.parent.mkdir(parents=True)
        stale.write_text("{}")
        (into / "authored" / "gone.json").write_text("[]")
        (into / "README.md").write_text("mine")
        export.write(self.render(), into)
        self.assertFalse(stale.exists())
        self.assertFalse((into / "authored" / "gone.json").exists())
        self.assertEqual((into / "README.md").read_text(), "mine")

    def test_export_returns_every_file(self):
        r = self.client.get("/record/export")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["files"], dict(self.render()))


if __name__ == "__main__":
    unittest.main()
