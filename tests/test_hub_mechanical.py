#!/usr/bin/env python3
"""sweep/hub/api/runs.py: the mechanical verbs -- inventory, materialise --adopt, encode, score, time, publish, abandon,
watch -- each a plan enqueued in one checked transaction; status reports the agents' heartbeats and identities.

    python3 -m unittest tests.test_hub_mechanical
"""
import json
import unittest

from tests.hub_helpers import client_for, fixture_store, post

B580, TI5060 = "intel-b580-ihd26.2.2-qsv-av1", "nvidia-5060ti-595-nvenc-av1"
CLASS, SEARCH, LANE = "native-1080p-sdr", "b580-qsv-av1", "m4-ipad-le1080p-sdr"
VIEWING = {"stage": "viewing", "content_class_id": CLASS, "encoder_unit_id": B580, "host": "media-01",
           "cells": [{"window_id": "tng", "settings": {"qsv.q": "30", "qsv.preset": "4", "qsv.b_strategy": "0"}},
                     {"window_id": "parks", "settings": {"qsv.q": "34", "qsv.preset": "4", "qsv.b_strategy": "0"}}]}


class Mechanical(unittest.TestCase):
    def setUp(self):
        self.store = fixture_store()
        self.addCleanup(self.store.close)
        self.client = client_for(self.store)
        self.queue = self.client.app.state.queue

    def run_row(self, run_id):
        return next(r for r in self.store.rows("run") if r["run_id"] == run_id)

    def test_inventory_plans_and_enqueues(self):
        status, text = post(self.client, "/runs/inventory", {"host": "media-01", "library": "movies", "titles": [{"title_id": "x", "path": "/media/x.mkv"}]})
        self.assertEqual(status, 200, text)
        run_id = json.loads(text)["run_id"]
        self.assertTrue(run_id.startswith("inventory-media-01-"))
        self.assertEqual(self.run_row(run_id)["state"], "planned")
        pending = self.queue.pending("media-01")
        self.assertEqual((len(pending), pending[0].run_id, pending[0].plan["run"]["stage"], pending[0].plan["inputs"][0]["title_id"]), (1, run_id, "inventory", "x"))

    def test_materialise_adopts_and_refuses_cutting(self):
        body = {"host": "eta", "encoder_unit_id": TI5060, "reference_set_id": "stage-1080p", "geometry": "1920x1080", "pix_fmt": "p010le",
                "chain_lane": LANE, "chain_host": "media-01", "chain_unit": B580,
                "cuts": [{"window_id": "tng", "kind": "reference", "path": r"D:\sweep\stage-1080p\tng.ref.mkv"}]}
        status, text = post(self.client, "/runs/materialise", body)
        self.assertEqual(status, 200, text)
        self.assertEqual(self.run_row(json.loads(text)["run_id"])["stage"], "materialise")
        status, text = post(self.client, "/runs/materialise", dict(body, adopt=False))
        self.assertEqual((status, text), (422, "REFUSING: materialise without --adopt cuts the windows, and cutting is M4's -- pass --adopt with the existing cuts' paths"))

    def test_encode_plans_a_search_run_or_a_viewing_run_and_refuses_a_mix(self):
        status, text = post(self.client, "/runs/encode", {"search_id": SEARCH, "host": "media-01", "windows": ["tng"], "rungs": [24, 30], "arms": ["arm-a"]})
        self.assertEqual(status, 200, text)
        run_id = json.loads(text)["run_id"]
        self.assertEqual((self.run_row(run_id)["stage"], len([c for c in self.store.rows("cell") if c["run_id"] == run_id])), ("encode", 2))
        status, text = post(self.client, "/runs/encode", VIEWING)
        self.assertEqual(status, 200, text)
        self.assertEqual(self.run_row(json.loads(text)["run_id"])["stage"], "viewing")
        status, text = post(self.client, "/runs/encode", dict(VIEWING, search_id=SEARCH))
        self.assertEqual((status, text), (422, "REFUSING: encode takes a search with --windows, --rungs and --arms, or a viewing's class, unit and cells, not both -- drop one"))
        status, text = post(self.client, "/runs/encode", dict(VIEWING, stage="screen"))
        self.assertEqual(status, 422)
        self.assertIn("screen", text)
        status, text = post(self.client, "/runs/encode", {"host": "media-01"})
        self.assertEqual(status, 422)

    def test_score_time_and_publish_enqueue_their_jobs(self):
        status, text = post(self.client, "/runs/score", {"run_id": "b580-viewing"})
        self.assertEqual(status, 200, text)
        self.assertTrue(json.loads(text)["run_id"].startswith("score-b580-viewing-"))
        self.assertEqual(self.queue.pending("media-01-score")[0].plan["score"]["height"], 1548)
        status, text = post(self.client, "/runs/time", {"run_id": "b580-viewing"})
        self.assertEqual(status, 200, text)
        self.assertEqual(self.run_row(json.loads(text)["run_id"])["stage"], "time")
        status, text = post(self.client, "/runs/publish", {"run_id": "b580-viewing"})
        self.assertEqual(status, 200, text)
        entry = next(e for e in self.queue.pending("media-01") if e.kind == "publish")
        self.assertEqual((entry.entry_id, entry.run_id, len(entry.plan["files"])), (json.loads(text)["entry_id"], None, 3))

    def test_the_refusals_reach_the_client_as_422_text(self):
        for path, body, word in [("/runs/inventory", {"host": "nope", "library": "m", "titles": []}, "host"),
                                 ("/runs/score", {"run_id": "b580-qsv-av1"}, "kept"),
                                 ("/runs/publish", {"run_id": "b580-qsv-av1"}, "kept"),
                                 ("/runs/score", {"run_id": "b580-viewing", "scorer": "eta"}, "scorer"),
                                 ("/runs/encode", {"search_id": SEARCH, "host": "media-01", "windows": ["sopranos"]}, "sopranos")]:
            status, text = post(self.client, path, body)
            self.assertEqual(status, 422, (path, text))
            self.assertTrue(text.startswith("REFUSING: ") and word in text, text)
        self.store.conn.execute("INSERT INTO content_class_lane VALUES ('native-1080p-sdr', 'kids-ipad-standard-sdr')")
        status, text = post(self.client, "/runs/time", {"run_id": "b580-viewing"})
        self.assertEqual((status, text), (422, "REFUSING: native-1080p-sdr serves kids-ipad-standard-sdr, m4-ipad-le1080p-sdr -- time --lane names which chain to time"))

    def test_abandon_posts_the_event_and_refuses_a_finished_run(self):
        _, text = post(self.client, "/runs/encode", VIEWING)
        run_id = json.loads(text)["run_id"]
        status, text = post(self.client, f"/runs/{run_id}/abandon", {"reason": "wrong settings"})
        self.assertEqual((status, text), (200, '{"ok":true}'))
        self.assertEqual(self.run_row(run_id)["state"], "abandoned")
        event = [e for e in self.store.rows("run_event") if e["run_id"] == run_id][-1]
        self.assertEqual((event["state"], event["detail"], event["by"]), ("abandoned", "wrong settings", "hub"))
        status, text = post(self.client, "/runs/b580-qsv-av1/abandon", {"reason": "x"})
        self.assertEqual((status, text), (422, "REFUSING: run b580-qsv-av1 is complete -- a finished run is not abandoned"))

    def test_watch_replays_the_events_as_server_sent_events(self):
        with self.client.stream("GET", "/runs/b580-qsv-av1/watch", params={"timeout": 0}) as r:
            self.assertEqual(r.headers["content-type"].split(";")[0], "text/event-stream")
            lines = [l for l in r.iter_lines() if l.startswith("data: ")]
        events = [json.loads(l[6:]) for l in lines]
        self.assertEqual([e["state"] for e in events], ["launched", "running", "complete"])
        self.assertEqual(events[-1]["final"], True)
        _, text = post(self.client, "/runs/encode", VIEWING)
        run_id = json.loads(text)["run_id"]
        with self.client.stream("GET", f"/runs/{run_id}/watch", params={"timeout": 0}) as r:
            lines = [l for l in r.iter_lines() if l.startswith("data: ")]
        self.assertEqual([json.loads(l[6:])["state"] for l in lines], ["planned"])

    def test_status_reports_heartbeats_and_identities(self):
        self.queue.beat("media-01", {"run_id": None, "cells_done": 0, "cells_total": 0}, 90)
        r = self.client.get("/runs/status").json()
        self.assertEqual(r["heartbeats"]["media-01"]["cells_done"], 0)
        self.assertIsNone(r["heartbeats"]["eta"])
        self.assertEqual(r["identities"]["media-01"], "node-encode:0.0.2.dev0+g0")
        self.assertIsNone(r["identities"]["htpc-01"])


if __name__ == "__main__":
    unittest.main()
