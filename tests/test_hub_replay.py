#!/usr/bin/env python3
"""The acceptance: the proof's fixture, built through the API and the ingest path in the campaign's order, with no
check firing at any step, and the authored tables equal to the fixture's row for row. Derived ladders
(arm_ladder_rung) are M4's derive-ladders and are omitted; the score run scores the 88 cells that encoded.

    python3 -m unittest tests.test_hub_replay
"""
import pathlib
import sys
import tempfile
import time
import unittest

from sweep.hub import export, ingest, store as st
from sweep.hub.store import Store
from tests import replay_data
from tests.hub_helpers import authored_tables, client_for, fixture_store, post


def replay(store):
    """Every step, in order; a refused step fails with the hub's text."""
    client = client_for(store)
    for step in replay_data.steps():
        kind = step[0]
        if kind == "verb":
            _, path, body = step
            status, text = post(client, path, body)
            if status != 200:
                raise AssertionError(f"{path} {body.get('lane', body.get('host', ''))}: {text}")
        elif kind == "identity":
            _, row = step
            with store.transaction() as conn:
                st.insert(conn, "host_identity", row)
        elif kind == "run":
            _, plan, started, finished, records = step
            with store.transaction() as conn:
                st.plan_run(conn, plan)
                st.post_event(conn, plan.run_id, started, "launched", "claimed; the agent reports the artifact the plan names")
                st.post_event(conn, plan.run_id, started + ":30", "running", "first cell started", by="agent")
                for record in records:
                    ingest.ingest_record(conn, plan.run_id, ingest.parse_record(record))
                st.post_event(conn, plan.run_id, finished, "complete", "count and heights verified against the plan")
        else:
            _, name, run_id, value, computed_at = step
            with store.transaction() as conn:
                st.calibrate(conn, name, run_id, value, computed_at)
    return client


class Replay(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.store = Store()
        t0 = time.perf_counter()
        cls.client = replay(cls.store)
        cls.seconds = time.perf_counter() - t0
        print(f"\nreplay: {len(replay_data.steps())} steps in {cls.seconds:.1f} s", file=sys.stderr)

    @classmethod
    def tearDownClass(cls):
        cls.store.close()

    def test_fixture_replays_through_the_api_with_no_firing_check(self):
        self.assertEqual(self.store.check(), {})
        expected = fixture_store()
        self.addCleanup(expected.close)
        got, want = authored_tables(self.store), authored_tables(expected)
        for table in want:
            with self.subTest(table=table):
                self.assertEqual(got[table], want[table])
        self.assertEqual(got, want)

    def test_reexport_of_unchanged_state_is_an_empty_diff(self):
        openapi = self.client.app.openapi()
        with self.store.reading() as conn:
            first = export.render(conn, openapi)
            second = export.render(conn, openapi)
        self.assertEqual(first, second)
        into = pathlib.Path(tempfile.mkdtemp())
        export.write(first, into)
        before = {p: p.read_bytes() for p in into.rglob("*") if p.is_file()}
        export.write(second, into)
        self.assertEqual({p: p.read_bytes() for p in into.rglob("*") if p.is_file()}, before)


if __name__ == "__main__":
    unittest.main()
