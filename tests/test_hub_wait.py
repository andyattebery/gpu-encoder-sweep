#!/usr/bin/env python3
"""sweep/hub/wait.py: the hub's own loop -- an expired heartbeat fails an active run with the count reached, and an ack
completes a run only when its records match the plan, stage by stage.

    python3 -m unittest tests.test_hub_wait
"""
import datetime as dt
import json
import unittest

from sweep.hub import exchange, ingest, planner, store as st, wait
from sweep.hub.queue import FakeQueue
from tests.hub_helpers import fixture_store

B580, CLASS = "intel-b580-ihd26.2.2-qsv-av1", "native-1080p-sdr"
NOW = dt.datetime(2026, 9, 7, 10, 0, 0, tzinfo=dt.timezone.utc)   # before real time: the hub stamps later events from its clock
CELLS = [{"window_id": "tng", "settings": {"qsv.q": "30", "qsv.preset": "4", "qsv.b_strategy": "0"}},
         {"window_id": "parks", "settings": {"qsv.q": "34", "qsv.preset": "4", "qsv.b_strategy": "0"}}]


def encode_record(key, kept=True):
    return {"kind": "encode", "cell_key": key, "bytes": 52000000, "bitrate_kbps": 6900.0, "frames": 1439, "duration_s": 60.0, "decode_path": "software", "kept": kept}


class Waiter(unittest.TestCase):
    def setUp(self):
        self.store = fixture_store()
        self.addCleanup(self.store.close)
        self.now = 0.0
        self.queue = FakeQueue(clock=lambda: self.now)
        with self.store.reading() as conn:
            self.plan, self.body = planner.plan_viewing(conn, CLASS, B580, "media-01", CELLS, NOW)
        planner.enqueue(self.store, self.queue, self.plan, self.body)

    def launch(self):
        with self.store.transaction() as conn:
            st.post_event(conn, self.plan.run_id, "2026-09-07T10:01:00", "launched", "claimed")

    def state(self):
        return next(r for r in self.store.rows("run") if r["run_id"] == self.plan.run_id)

    def test_an_expired_heartbeat_fails_the_run_with_the_count_reached(self):
        self.launch()
        self.queue.beat("media-01", {"run_id": self.plan.run_id, "cells_done": 1, "cells_total": 2}, 90)
        with self.store.transaction() as conn:
            ingest.ingest_record(conn, self.plan.run_id, ingest.parse_record(encode_record(self.plan.cells[0].cell_key)))
        self.assertEqual(wait.sweep_once(self.store, self.queue), [])                  # alive: nothing happens
        self.now += 91
        self.assertEqual(wait.sweep_once(self.store, self.queue), [self.plan.run_id])
        run = self.state()
        self.assertEqual(run["state"], "failed")
        self.assertIsNotNone(run["finished_at"])
        event = [e for e in self.store.rows("run_event") if e["run_id"] == self.plan.run_id][-1]
        self.assertEqual((event["state"], event["detail"], event["by"]), ("failed", "heartbeat expired; 1 of 2 cells done", "hub"))
        self.assertEqual(wait.sweep_once(self.store, self.queue), [])                  # failed once, not again
        self.assertEqual(self.store.check(), {})

    def test_a_planned_run_and_a_finished_run_are_not_the_waiters_business(self):
        self.assertEqual(wait.sweep_once(self.store, self.queue), [])                  # planned, never launched: no heartbeat is expected yet
        self.assertEqual(self.state()["state"], "planned")

    def test_verify_at_ack_per_stage(self):
        self.launch()
        with self.store.reading() as conn:
            run = {"run_id": self.plan.run_id, "stage": "viewing", "parent_run_id": None}
            self.assertEqual(wait.verify_at_ack(conn, run, self.body), "2 of 2 cells have no record")
        with self.store.transaction() as conn:
            ingest.ingest_record(conn, self.plan.run_id, ingest.parse_record(encode_record(self.plan.cells[0].cell_key)))
            ingest.ingest_record(conn, self.plan.run_id, ingest.parse_record({"kind": "failure", "cell_key": self.plan.cells[1].cell_key, "at": "2026-09-07T10:05", "stderr": "boom", "rc": 1}))
        with self.store.reading() as conn:
            self.assertIsNone(wait.verify_at_ack(conn, run, self.body))               # a failure is a record too
            score = {"run_id": "b580-viewing-score", "stage": "score", "parent_run_id": "b580-viewing"}
            self.assertIsNone(wait.verify_at_ack(conn, score, {}))                      # the fixture scored all three kept encodes
            self.assertEqual(wait.verify_at_ack(conn, {"run_id": "x", "stage": "score", "parent_run_id": "b580-viewing"}, {}), "3 of 3 kept encodes have no score under this run")
            inv = {"run_id": "b580-inventory", "stage": "inventory", "parent_run_id": None}
            self.assertIsNone(wait.verify_at_ack(conn, inv, {"inputs": [{"title_id": "mrrobot", "path": "/x"}]}))
            self.assertEqual(wait.verify_at_ack(conn, inv, {"inputs": [{"title_id": "mrrobot", "path": "/x"}, {"title_id": "nope", "path": "/y"}]}), "1 of 2 titles have no record: nope")
            mat = {"run_id": "b580-materialise", "stage": "materialise", "parent_run_id": None}
            self.assertEqual(wait.verify_at_ack(conn, mat, {"cuts": [{"cut_id": "tng.ref"}, {"cut_id": "zzz.ref"}]}), "1 of 2 cuts have no record: zzz.ref")
            time_run = {"run_id": "b580-qsv-av1-time", "stage": "time", "parent_run_id": None}
            self.assertIsNone(wait.verify_at_ack(conn, time_run, {"cells": [{"cell_key": "t-tng", "repeats": 2}]}))
            self.assertEqual(wait.verify_at_ack(conn, time_run, {"cells": [{"cell_key": "t-tng", "repeats": 9}]}), "1 of 1 cells are short of their 9 repeats: t-tng")

    def test_verify_publish_job(self):
        with self.store.transaction() as conn:
            exchange.record_publish(conn, "runs/b580-viewing/enc/g-a.mkv", "media-01", 1, "s" * 64, "2026-09-08T10:00", run_id="b580-viewing", cell_key="g-a")
        job = {"kind": "publish", "by_host": "media-01", "files": [{"relative": "runs/b580-viewing/enc/g-a.mkv"}, {"relative": "runs/b580-viewing/enc/g-b.mkv"}]}
        with self.store.reading() as conn:
            self.assertEqual(wait.verify_publish(conn, job), "1 of 2 files are not on the share: runs/b580-viewing/enc/g-b.mkv")


if __name__ == "__main__":
    unittest.main()
