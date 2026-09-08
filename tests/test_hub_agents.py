#!/usr/bin/env python3
"""sweep/hub/api/agents.py: what an agent speaks to the hub -- config, heartbeat, claim, events, records, frames, ack,
abandon, and the exchange's published -- each refusing what the record refuses, with the hub completing a run at ack.

    python3 -m unittest tests.test_hub_agents
"""
import gzip
import json
import pathlib
import tempfile
import unittest

from sweep.hub import exchange, store as st
from sweep.hub.queue import FakeQueue
from tests.hub_helpers import client_for, fixture_store, post

B580, CLASS = "intel-b580-ihd26.2.2-qsv-av1", "native-1080p-sdr"
VIEWING = {"stage": "viewing", "content_class_id": CLASS, "encoder_unit_id": B580, "host": "media-01",
           "cells": [{"window_id": "tng", "settings": {"qsv.q": "30", "qsv.preset": "4", "qsv.b_strategy": "0"}},
                     {"window_id": "parks", "settings": {"qsv.q": "34", "qsv.preset": "4", "qsv.b_strategy": "0"}}]}
IDENTITY = {"artifact": "node-encode:0.0.2.dev0+g0", "harness_version": "g0", "ffmpeg_build": "8.1.2-Jellyfin", "ffmpeg_sha": "0b0ea2d",
            "ffmpeg_filters": ["scale", "format", "hwupload", "scale_cuda"], "ffvship_version": None, "free_bytes": 850000000000}


def encode_record(key, kept=True, frames=1439):
    return {"kind": "encode", "cell_key": key, "bytes": 52000000, "bitrate_kbps": 6900.0, "frames": frames, "duration_s": 60.0, "decode_path": "software", "kept": kept}


class Agents(unittest.TestCase):
    def setUp(self):
        self.store = fixture_store()
        self.addCleanup(self.store.close)
        self.now = 0.0
        self.queue = FakeQueue(clock=lambda: self.now)
        self.share = pathlib.Path(tempfile.mkdtemp())
        self.frames = pathlib.Path(tempfile.mkdtemp())
        self.client = client_for(self.store, queue=self.queue, share=self.share, frames=self.frames)

    def plan_viewing(self):
        status, text = post(self.client, "/runs/encode", VIEWING)
        self.assertEqual(status, 200, text)
        return json.loads(text)["run_id"]

    def run_row(self, run_id):
        return next(r for r in self.store.rows("run") if r["run_id"] == run_id)

    def events(self, run_id):
        return [(e["state"], e["by"]) for e in self.store.rows("run_event") if e["run_id"] == run_id]

    def test_config_gives_the_host_and_its_scorer(self):
        r = self.client.get("/agents/media-01-score/config").json()
        self.assertEqual((r["host"]["work_root"], r["host"]["local_view"], r["heartbeat_s"], r["ttl_s"]), ("/mnt/data/sweep-score", "/mnt/data/sweep", 30, 90))
        self.assertEqual(r["scorer"]["ffvship"], ["/usr/local/bin/FFVship"])
        self.assertIsNone(self.client.get("/agents/media-01/config").json()["scorer"])
        self.assertEqual(self.client.get("/agents/nope/config").status_code, 422)

    def test_heartbeat_is_kept_with_its_ttl_and_a_changed_identity_is_recorded(self):
        body = {"run_id": None, "cells_done": 0, "cells_total": 0, "artifact": IDENTITY["artifact"], "identity": IDENTITY}
        self.assertEqual(post(self.client, "/agents/media-01/heartbeat", body), (200, '{"ok":true,"run_state":null}'))
        self.assertEqual(self.queue.pulse("media-01")["cells_done"], 0)
        before = len([r for r in self.store.rows("host_identity") if r["host"] == "media-01"])
        newer = dict(IDENTITY, artifact="node-encode:0.0.3.dev1+gabc", harness_version="gabc")
        post(self.client, "/agents/media-01/heartbeat", dict(body, artifact=newer["artifact"], identity=newer))
        self.assertEqual(len([r for r in self.store.rows("host_identity") if r["host"] == "media-01"]), before + 1)
        run_id = self.plan_viewing()
        status, text = post(self.client, "/agents/media-01/heartbeat", dict(body, run_id=run_id))
        self.assertEqual(json.loads(text)["run_state"], "planned")

    def test_claim_launches_a_planned_run_and_hands_its_cells_with_done(self):
        run_id = self.plan_viewing()
        self.assertEqual(self.client.post("/agents/eta/claim", json={"block_s": 0}).status_code, 204)
        r = self.client.post("/agents/media-01/claim", json={"block_s": 0})
        self.assertEqual(r.status_code, 200, r.text)
        claim = r.json()
        self.assertEqual((claim["kind"], claim["run"]["run_id"], len(claim["cells"]), claim["done"]), ("run", run_id, 2, []))
        self.assertEqual(self.run_row(run_id)["state"], "launched")
        self.assertEqual(self.events(run_id), [("planned", "hub"), ("launched", "hub")])
        self.assertTrue(claim["entry_id"])
        r = self.client.post("/agents/media-01/claim", json={"block_s": 0})              # the agent restarted: the same entry, launched again
        self.assertEqual(r.json()["run"]["run_id"], run_id)

    def test_claim_refuses_another_artifact_and_leaves_the_entry_pending(self):
        run_id = self.plan_viewing()
        self.store.conn.execute("INSERT INTO host_identity VALUES ('media-01','2026-09-07T11:00','node-encode:0.0.3.dev1+gabc','gabc','8.1.2-Jellyfin','0b0ea2d','[]',NULL,1)")
        r = self.client.post("/agents/media-01/claim", json={"block_s": 0})
        self.assertEqual((r.status_code, r.text), (409, f"REFUSING: run {run_id} was planned for artifact node-encode:0.0.2.dev0+g0; media-01 reports node-encode:0.0.3.dev1+gabc -- update the node to the plan's artifact, or abandon and re-plan"))
        self.assertEqual(self.run_row(run_id)["state"], "planned")
        self.assertEqual(len(self.queue.pending("media-01")), 1)

    def test_claim_of_a_finished_runs_stale_entry_acks_it_away(self):
        self.queue.enqueue("media-01", "b580-qsv-av1", {"run": {"run_id": "b580-qsv-av1"}, "cells": []})
        self.assertEqual(self.client.post("/agents/media-01/claim", json={"block_s": 0}).status_code, 204)
        self.assertEqual(self.queue.pending("media-01"), [])
        self.assertEqual(self.run_row("b580-qsv-av1")["state"], "complete")

    def test_a_timing_run_waits_until_its_machine_is_quiet(self):
        _, text = post(self.client, "/runs/time", {"run_id": "b580-viewing"})
        run_id = json.loads(text)["run_id"]
        with self.store.transaction() as conn:
            st.post_event(conn, "b580-qsv-av1-screen", "2026-09-07T09:00", "running", "first cell started", by="agent")
        self.assertEqual(self.client.post("/agents/media-01/claim", json={"block_s": 0}).status_code, 204)
        self.assertEqual((self.run_row(run_id)["state"], len(self.queue.pending("media-01"))), ("planned", 1))
        with self.store.transaction() as conn:
            st.post_event(conn, "b580-qsv-av1-screen", "2026-09-07T09:30", "complete", "done")
        self.assertEqual(self.client.post("/agents/media-01/claim", json={"block_s": 0}).json()["run"]["run_id"], run_id)

    def test_records_are_ingested_in_one_transaction_and_published(self):
        run_id = self.plan_viewing()
        claim = self.client.post("/agents/media-01/claim", json={"block_s": 0}).json()
        keys = [c["cell_key"] for c in claim["cells"]]
        self.assertEqual(post(self.client, f"/runs/{run_id}/records", {"records": [encode_record(keys[0])]}), (200, '{"ok":true,"ingested":1}'))
        self.assertEqual(len([e for e in self.store.rows("encode") if e["cell_key"] == keys[0]]), 1)
        status, text = post(self.client, f"/runs/{run_id}/records", {"records": [encode_record(keys[1]), encode_record(keys[1], frames=3)]})
        self.assertEqual(status, 422)
        self.assertIn("REFUSING: ", text)
        self.assertEqual(len([e for e in self.store.rows("encode") if e["cell_key"] == keys[1]]), 0)   # the batch rolled back
        heard = list(self.queue.listen(run_id, 0))                                                   # nothing after subscription; publishes happened earlier
        self.assertEqual(heard, [])
        r = self.client.post("/agents/media-01/claim", json={"block_s": 0}).json()
        self.assertEqual(r["done"], [keys[0]])

    def test_events_from_an_agent_are_running_or_failed_stamped_by_the_hub(self):
        run_id = self.plan_viewing()
        self.client.post("/agents/media-01/claim", json={"block_s": 0})
        self.assertEqual(post(self.client, f"/runs/{run_id}/events", {"state": "running", "detail": "first cell started"}), (200, '{"ok":true}'))
        self.assertEqual(self.events(run_id)[-1], ("running", "agent"))
        status, text = post(self.client, f"/runs/{run_id}/events", {"state": "complete", "detail": "done"})
        self.assertEqual((status, text), (422, "REFUSING: an agent may post running or failed -- the hub completes a run at ack"))
        self.assertEqual(self.store.check(), {})

    def test_ack_completes_when_every_cell_has_a_record_and_fails_otherwise(self):
        run_id = self.plan_viewing()
        claim = self.client.post("/agents/media-01/claim", json={"block_s": 0}).json()
        keys = [c["cell_key"] for c in claim["cells"]]
        post(self.client, f"/runs/{run_id}/records", {"records": [encode_record(keys[0])]})
        status, text = post(self.client, f"/runs/{run_id}/ack", {"entry_id": claim["entry_id"]})
        self.assertEqual((status, text), (200, '{"ok":true,"state":"failed"}'))
        self.assertEqual(self.events(run_id)[-1], ("failed", "hub"))
        self.assertEqual([e["detail"] for e in self.store.rows("run_event") if e["run_id"] == run_id][-1], "acked with 1 of 2 cells have no record")
        self.assertEqual(self.queue.pending("media-01"), [])
        other = dict(VIEWING, cells=[dict(c, settings=dict(c["settings"], **{"qsv.q": "40"})) for c in VIEWING["cells"]])
        status, text = post(self.client, "/runs/encode", other)                                     # the same windows at another anchor: new cells
        run_id = json.loads(text)["run_id"]
        claim = self.client.post("/agents/media-01/claim", json={"block_s": 0}).json()
        self.assertEqual(claim["done"], [])
        post(self.client, f"/runs/{run_id}/records", {"records": [encode_record(c["cell_key"]) for c in claim["cells"]]})
        status, text = post(self.client, f"/runs/{run_id}/ack", {"entry_id": claim["entry_id"]})
        self.assertEqual((status, text), (200, '{"ok":true,"state":"complete"}'))
        run = self.run_row(run_id)
        self.assertEqual((run["state"], run["verified_at"] is not None), ("complete", True))
        self.assertEqual(self.store.check(), {})

    def test_abandon_is_read_between_cells(self):
        run_id = self.plan_viewing()
        self.assertEqual(self.client.get(f"/runs/{run_id}/abandon").json(), {"abandon": False, "reason": None})
        post(self.client, f"/runs/{run_id}/abandon", {"reason": "wrong settings"})
        self.assertEqual(self.client.get(f"/runs/{run_id}/abandon").json(), {"abandon": True, "reason": "wrong settings"})

    def test_frames_are_kept_gzipped_beside_the_store_and_never_overwritten(self):
        body = {"cell_key": "g-a", "metric": "ssimulacra2", "values": [80.0, 81.5, 79.25]}
        self.assertEqual(post(self.client, "/runs/b580-viewing-score/frames", body), (200, '{"ok":true}'))
        path = self.frames / "b580-viewing-score" / "g-a.1548.ssimulacra2.json.gz"
        self.assertEqual(json.loads(gzip.open(path).read()), [80.0, 81.5, 79.25])
        self.assertEqual(post(self.client, "/runs/b580-viewing-score/frames", body)[0], 200)             # identical: a no-op
        status, text = post(self.client, "/runs/b580-viewing-score/frames", dict(body, values=[1.0]))
        self.assertEqual((status, text), (422, "REFUSING: b580-viewing-score already holds frames for g-a ssimulacra2 that differ -- a record is never overwritten; abandon the run and re-plan"))

    def test_published_is_verified_at_the_pool_path_then_recorded(self):
        data = b"z" * 5000
        (self.share / "runs" / "b580-viewing" / "enc").mkdir(parents=True)
        (self.share / "runs" / "b580-viewing" / "enc" / "g-a.mkv").write_bytes(data)
        body = {"path": "runs/b580-viewing/enc/g-a.mkv", "by_host": "media-01", "bytes": 5000, "sha256": exchange.sha256_file(self.share / "runs" / "b580-viewing" / "enc" / "g-a.mkv"),
                "run_id": "b580-viewing", "cell_key": "g-a", "cut_id": None}
        self.assertEqual(post(self.client, "/exchange/published", dict(body, sha256="0" * 64))[0], 422)
        self.assertEqual(post(self.client, "/exchange/published", body), (200, '{"ok":true}'))
        self.assertEqual(len([p for p in self.store.rows("published") if p["cell_key"] == "g-a"]), 1)
        status, text = post(self.client, "/agents/media-01/ack", {"entry_id": "nope"})
        self.assertEqual(status, 422)

    def test_a_publish_job_is_claimed_and_acked_once_every_file_is_on_the_share(self):
        _, text = post(self.client, "/runs/publish", {"run_id": "b580-viewing"})
        entry_id = json.loads(text)["entry_id"]
        claim = self.client.post("/agents/media-01/claim", json={"block_s": 0}).json()
        self.assertEqual((claim["kind"], claim["entry_id"], len(claim["files"]), claim["done"]), ("publish", entry_id, 3, []))
        status, text = post(self.client, "/agents/media-01/ack", {"entry_id": entry_id})
        self.assertEqual((status, text), (422, "REFUSING: 3 of 3 files are not on the share: runs/b580-viewing/enc/g-a.mkv, runs/b580-viewing/enc/g-b.mkv, runs/b580-viewing/enc/g-i.mkv -- publish them before the ack"))
        for key in ("g-a", "g-b", "g-i"):
            (self.share / "runs" / "b580-viewing" / "enc").mkdir(parents=True, exist_ok=True)
            f = self.share / "runs" / "b580-viewing" / "enc" / f"{key}.mkv"
            f.write_bytes(key.encode() * 100)
            post(self.client, "/exchange/published", {"path": f"runs/b580-viewing/enc/{key}.mkv", "by_host": "media-01", "bytes": 300, "sha256": exchange.sha256_file(f),
                                                       "run_id": "b580-viewing", "cell_key": key, "cut_id": None})
        self.assertEqual(post(self.client, "/agents/media-01/ack", {"entry_id": entry_id}), (200, '{"ok":true}'))
        self.assertEqual(self.queue.pending("media-01"), [])


if __name__ == "__main__":
    unittest.main()
