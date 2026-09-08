#!/usr/bin/env python3
"""sweep/node/agent.py: the loop -- claim, run each cell not yet recorded, post as it goes, ack -- proven end to end
against the in-process hub with the fake tools: a two-cell run completes, a killed agent's run reads failed and resumes
idempotently, another artifact is refused, abandon stops it between cells, a score run and a time run land their rows,
a publish job puts files on the share with a sha both ends.

    python3 -m unittest tests.test_node_agent
"""
import gzip
import json
import os
import pathlib
import unittest
from unittest import mock

from sweep.hub import wait
from tests.hub_helpers import post
from tests.node_helpers import CLASS, UNIT, hub_and_agent, make_sample

VIEW = {"stage": "viewing", "content_class_id": CLASS, "encoder_unit_id": UNIT, "host": "enc",
        "cells": [{"window_id": "tng", "settings": {"qsv.q": "30", "qsv.preset": "4", "qsv.b_strategy": "0"}},
                  {"window_id": "parks", "settings": {"qsv.q": "34", "qsv.preset": "4", "qsv.b_strategy": "0"}}]}


class EndToEnd(unittest.TestCase):
    def setUp(self):
        self.p = hub_and_agent()
        self.agent = make_sample(self.p)

    def plan(self, path, body):
        status, text = post(self.p.client, path, body)
        self.assertEqual(status, 200, text)
        return json.loads(text)

    def events(self, run_id):
        return [(e["state"], e["by"]) for e in self.p.store.rows("run_event") if e["run_id"] == run_id]

    def test_the_sample_came_through_the_agent(self):
        self.assertEqual(sorted(t["title_id"] for t in self.p.store.rows("title")), ["parks", "tng"])
        self.assertEqual(sorted(c["cut_id"] for c in self.p.store.rows("cut")), ["parks.ref", "parks.src", "tng.ref", "tng.src"])
        self.assertTrue((self.p.dirs["work"] / "refsets" / "rs" / "tng.reference.mkv").is_file())
        self.assertEqual({r["state"] for r in self.p.store.rows("run")}, {"complete"})
        self.assertEqual(len([i for i in self.p.store.rows("host_identity") if i["host"] == "enc"]), 1)     # the agent reported itself once
        self.assertEqual(self.p.store.check(), {})

    def test_a_two_cell_viewing_run_completes_with_its_records(self):
        run_id = self.plan("/runs/encode", VIEW)["run_id"]
        self.assertEqual(self.agent.serve_once(), run_id)
        run = self.p.run_row(run_id)
        self.assertEqual((run["state"], run["verified_at"] is not None), ("complete", True))
        self.assertEqual(self.events(run_id), [("planned", "hub"), ("launched", "hub"), ("running", "agent"), ("complete", "hub")])
        encodes = [e for e in self.p.store.rows("encode") if e["cell_key"] in {c["cell_key"] for c in self.p.store.rows("cell") if c["run_id"] == run_id}]
        self.assertEqual(len(encodes), 2)
        self.assertTrue(all(e["frames"] == 1439 and e["kept"] == 1 for e in encodes))
        self.assertEqual(len(list((self.p.dirs["work"] / "runs" / run_id / "enc").glob("*.mkv"))), 2)
        self.assertEqual(len(list((self.p.dirs["work"] / "runs" / run_id / "records").glob("*.json"))), 2)
        self.assertIsNone(self.agent.serve_once())
        status = self.p.client.get("/runs/status").json()
        self.assertEqual(status["heartbeats"]["enc"]["run_id"], None)
        self.assertEqual(self.p.store.check(), {})

    def test_a_killed_agent_reads_failed_and_a_restart_resumes_idempotently(self):
        run_id = self.plan("/runs/encode", VIEW)["run_id"]
        self.agent.serve_once(die_after=1)                                                            # killed after its first record
        self.assertEqual(self.p.run_row(run_id)["state"], "running")
        self.p.clock[0] += 91
        self.assertEqual(wait.sweep_once(self.p.store, self.p.queue), [run_id])
        run = self.p.run_row(run_id)
        self.assertEqual(run["state"], "failed")
        self.assertEqual([e["detail"] for e in self.p.store.rows("run_event") if e["run_id"] == run_id][-1], "heartbeat expired; 1 of 2 cells done")
        fresh = self.p.agent("enc")
        fresh.start()
        self.assertEqual(fresh.serve_once(), run_id)
        self.assertEqual(self.p.run_row(run_id)["state"], "complete")
        self.assertEqual(fresh.cells_run, 1)                                                          # only the cell without a record ran
        self.assertEqual(self.events(run_id)[-3:], [("launched", "hub"), ("running", "agent"), ("complete", "hub")])
        self.assertEqual(self.p.store.check(), {})

    def test_a_plan_for_another_artifact_is_refused_and_waits(self):
        run_id = self.plan("/runs/encode", VIEW)["run_id"]
        other = self.p.agent("enc")
        with mock.patch.dict(os.environ, {"SWEEP_ARTIFACT": "node-encode:9.9.9"}):
            other.config = other.config.__class__(hub=other.config.hub, token="", host="enc", artifact_override="node-encode:9.9.9")
            other.start()
        self.assertIsNone(other.serve_once())
        self.assertEqual(self.p.run_row(run_id)["state"], "planned")
        self.assertIn("was planned for artifact", other.log[-1])
        self.assertEqual(len(self.p.queue.pending("enc")), 1)

    def test_abandon_stops_between_cells(self):
        run_id = self.plan("/runs/encode", VIEW)["run_id"]
        before = self.agent.cells_run
        self.agent.before_cell = lambda n: post(self.p.client, f"/runs/{run_id}/abandon", {"reason": "changed my mind"}) if n == 1 else None
        self.assertEqual(self.agent.serve_once(), run_id)
        self.assertEqual((self.p.run_row(run_id)["state"], self.agent.cells_run - before), ("abandoned", 1))
        self.assertEqual(self.p.queue.pending("enc"), [])

    def test_a_score_run_lands_scores_steps_frames_and_discards_the_encodes(self):
        run_id = self.plan("/runs/encode", VIEW)["run_id"]
        self.agent.serve_once()
        scorer = self.p.agent("sco")
        scorer.start()                                                                                # a plan is built for what the scorer reports
        score_id = self.plan("/runs/score", {"run_id": run_id})["run_id"]
        self.assertEqual(scorer.serve_once(), score_id)
        self.assertEqual(self.p.run_row(score_id)["state"], "complete")
        scores = [s for s in self.p.store.rows("score") if s["run_id"] == score_id]
        self.assertEqual(len(scores), 2 * 8)                                                          # 2 cells x (ssimulacra2 mean, p5, min; butteraugli max; vmaf, cambi, psnr_y, float_ssim mean)
        self.assertTrue(all(s["height"] == 1548 and s["recipe"] == "S1" for s in scores))
        self.assertEqual(len([s for s in self.p.store.rows("step_trace") if s["run_id"] == score_id]), 2 * 5)
        self.assertEqual(len(list((self.p.dirs["work"] / "runs" / run_id / "enc").glob("*.mkv"))), 0)  # discarded once scored
        self.assertTrue(all(e["kept"] == 0 for e in self.p.store.rows("encode") if e["cell_key"].startswith(tuple(c["cell_key"] for c in self.p.store.rows("cell") if c["run_id"] == run_id))))
        frames = sorted(p.name for p in (self.p.root / "frames" / score_id).iterdir())
        self.assertEqual(len(frames), 4)
        self.assertEqual(len(json.loads(gzip.open(self.p.root / "frames" / score_id / frames[0]).read())), 1439)
        self.assertEqual(self.p.store.check(), {})

    def test_a_time_run_samples_every_configuration_with_a_warm_up(self):
        run_id = self.plan("/runs/encode", VIEW)["run_id"]
        self.agent.serve_once()
        time_id = self.plan("/runs/time", {"run_id": run_id, "repeats": 3})["run_id"]
        self.assertEqual(self.agent.serve_once(), time_id)
        self.assertEqual(self.p.run_row(time_id)["state"], "complete")
        keys = {c["cell_key"] for c in self.p.store.rows("cell") if c["run_id"] == time_id}
        samples = [t for t in self.p.store.rows("timing") if t["cell_key"] in keys]
        self.assertEqual((len(keys), len(samples)), (4, 4 * 3))                                       # every configuration on every window: 2 x 2, three repeats each
        self.assertEqual(sorted(s["is_warmup"] for s in samples), [0] * 8 + [1] * 4)
        self.assertEqual(self.p.store.check(), {})

    def test_a_publish_job_puts_the_files_on_the_share_with_a_sha_both_ends(self):
        run_id = self.plan("/runs/encode", VIEW)["run_id"]
        self.agent.serve_once()
        entry_id = self.plan("/runs/publish", {"run_id": run_id})["entry_id"]
        self.assertEqual(self.agent.serve_once(), f"publish:{entry_id}")
        published = [p for p in self.p.store.rows("published") if p["run_id"] == run_id]
        self.assertEqual(len(published), 2)
        for p in published:
            self.assertTrue((self.p.dirs["share"] / pathlib.Path(*p["path"].split("/"))).is_file())
        self.assertEqual(self.p.queue.pending("enc"), [])
        scorer = self.p.agent("sco")
        scorer.start()
        same = self.plan("/runs/score", {"run_id": run_id, "scorer": "sco"})                         # a scorer on the same machine views, no pull
        self.assertTrue(same["run_id"].startswith("score-"))

    def test_records_are_reposted_after_a_crash_before_the_post(self):
        run_id = self.plan("/runs/encode", VIEW)["run_id"]
        self.agent.serve_once(die_after=1, before_post=True)                                          # the record is on disk, the post never happened
        self.assertEqual(len([e for e in self.p.store.rows("encode")]), 0)
        self.assertEqual(len(list((self.p.dirs["work"] / "runs" / run_id / "records").glob("*.json"))), 1)
        fresh = self.p.agent("enc")
        fresh.start()
        self.assertEqual(fresh.serve_once(), run_id)
        self.assertEqual((self.p.run_row(run_id)["state"], fresh.cells_run, fresh.reposted), ("complete", 1, 1))


if __name__ == "__main__":
    unittest.main()
