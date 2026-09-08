#!/usr/bin/env python3
"""sweep/hub/planner.py: the stage planners turn the store's intent into run rows and a claim body, and enqueue writes
the rows first and the queue entry second. Every cell's argv is composed here; the agent runs what it is handed.

    python3 -m unittest tests.test_hub_planner
"""
import datetime as dt
import unittest

from sweep import recipes
from sweep.hub import exchange, ingest, planner, store as st
from sweep.hub.build import PLUMBING
from sweep.hub.queue import FakeQueue
from sweep.hub.refusals import Refusal
from tests.hub_helpers import fixture_store

B580, A4000, TI5060 = "intel-b580-ihd26.2.2-qsv-av1", "nvidia-a4000-595-nvenc-hevc", "nvidia-5060ti-595-nvenc-av1"
CLASS, SEARCH, LANE = "native-1080p-sdr", "b580-qsv-av1", "m4-ipad-le1080p-sdr"
NOW = dt.datetime(2026, 9, 8, 10, 0, 0, tzinfo=dt.timezone.utc)
FFMPEG = "8.1.2-Jellyfin"


def identity_settings(cell):
    return sorted((s, v) for s, v, role in cell.settings if role == "identity")


class Planners(unittest.TestCase):
    def setUp(self):
        self.store = fixture_store()
        self.addCleanup(self.store.close)
        self.queue = FakeQueue()

    def plan_in(self, fn, *args, **kw):
        with self.store.reading() as conn:
            return fn(conn, *args, **kw)

    # ---- the small ones
    def test_run_id_is_stage_subject_and_the_utc_stamp(self):
        self.assertEqual(planner.run_id("encode", SEARCH, NOW), "encode-b580-qsv-av1-20260908T100000Z")

    def test_default_resolved_fills_the_units_scoped_defaults_not_set_by_identity(self):
        with self.store.reading() as conn:
            rows = planner.default_resolved(conn, B580, {"qsv.q": "30", "qsv.preset": "4", "qsv.b_strategy": "0"})
        self.assertEqual(rows, [("generic.compression_level", "0", "default_resolved"), ("qsv.adaptive_b", "-1", "default_resolved")])

    def test_a_host_that_never_reported_an_identity_is_refused(self):
        with self.assertRaises(Refusal) as cm:
            self.plan_in(planner.plan_inventory, "htpc-01", "movies", [{"title_id": "x", "path": "/media/x.mkv"}], NOW)
        self.assertEqual(str(cm.exception), "REFUSING: htpc-01 has never reported an identity -- start its agent; a plan is built for the code the node runs")

    # ---- inventory
    def test_inventory_plans_a_unit_less_run_naming_its_titles(self):
        plan, body = self.plan_in(planner.plan_inventory, "media-01", "movies", [{"title_id": "x", "path": "/media/Movies/X.mkv"}], NOW)
        self.assertEqual((plan.stage, plan.encoder_unit_id, plan.content_class_id, plan.node_label, plan.artifact, plan.planned_at),
                         ("inventory", None, None, "media-01", "node-encode:0.0.2.dev0+g0", "2026-09-08T10:00:00+00:00"))
        self.assertEqual(plan.run_id, "inventory-media-01-20260908T100000Z")
        self.assertEqual(body["inputs"], [{"title_id": "x", "path": "/media/Movies/X.mkv", "library": "movies"}])
        self.assertEqual(body["run"]["tools"]["ffmpeg"], "/opt/jellyfin-ffmpeg/bin/ffmpeg")
        self.assertEqual(body["run"]["work_root"], "/mnt/data/sweep")
        self.assertEqual(body["run"]["recipes"], {"score": "S1", "key": "K1"})

    # ---- adopt
    def test_adopt_plans_a_materialise_run_with_the_chain_that_built_the_cuts(self):
        cuts = [{"window_id": "tng", "kind": "reference", "path": r"D:\sweep\stage-1080p\tng.ref.mkv"},
                {"window_id": "tng", "kind": "source", "path": r"D:\sweep\stage-1080p\tng.src.mkv"}]
        plan, body = self.plan_in(planner.plan_adopt, "eta", TI5060, "stage-1080p", "1920x1080", "p010le", (LANE, "media-01", B580), cuts, NOW)
        self.assertEqual((plan.stage, plan.encoder_unit_id, plan.windows, plan.node_label), ("materialise", TI5060, ("tng",), f"eta:{TI5060}"))
        self.assertEqual(body["reference_set"], {"id": "stage-1080p", "geometry": "1920x1080", "pix_fmt": "p010le", "post": False})
        self.assertEqual(body["cuts"][0], {"cut_id": "tng.ref", "window_id": "tng", "kind": "reference", "path": r"D:\sweep\stage-1080p\tng.ref.mkv",
                                           "dest": r"D:\sweep\refsets\stage-1080p\tng.reference.mkv",
                                           "chain": {"lane": LANE, "host": "media-01", "encoder_unit_id": B580}})
        self.assertEqual(body["cuts"][1]["chain"], None)
        _, fresh = self.plan_in(planner.plan_adopt, "eta", TI5060, "stage-new", "1920x1080", "p010le", (LANE, "media-01", B580), cuts, NOW)
        self.assertTrue(fresh["reference_set"]["post"])

    def test_adopt_refuses_a_unit_off_the_host_and_an_unknown_chain(self):
        cuts = [{"window_id": "tng", "kind": "source", "path": "/x"}]
        with self.assertRaises(Refusal) as cm:
            self.plan_in(planner.plan_adopt, "eta", B580, "stage-1080p", "1920x1080", "p010le", (LANE, "media-01", B580), cuts, NOW)
        self.assertIn("host_unit", str(cm.exception))
        with self.assertRaises(Refusal) as cm:
            self.plan_in(planner.plan_adopt, "eta", TI5060, "stage-1080p", "1920x1080", "p010le", (LANE, "htpc-01", B580), cuts, NOW)
        self.assertIn("chain", str(cm.exception))

    # ---- viewing
    def viewing(self, cells=None, host="media-01"):
        cells = cells or [{"window_id": "tng", "settings": {"qsv.q": "30", "qsv.preset": "4", "qsv.b_strategy": "0"}},
                          {"window_id": "parks", "settings": {"qsv.q": "34", "qsv.preset": "4", "qsv.b_strategy": "0"}}]
        return self.plan_in(planner.plan_viewing, CLASS, B580, host, cells, NOW)

    def test_viewing_plans_named_cells_with_k1_keys_and_default_resolved_rows(self):
        plan, body = self.viewing()
        self.assertEqual((plan.stage, plan.search_id, plan.windows, len(plan.cells)), ("viewing", None, ("parks", "tng"), 2))
        cell = plan.cells[0]
        self.assertEqual(cell.cell_key, recipes.cell_key(B580, "sha-tng-ref", "reference", "tng", [("qsv.q", "30"), ("qsv.preset", "4"), ("qsv.b_strategy", "0")], FFMPEG))
        self.assertEqual(sorted(cell.settings), [("generic.compression_level", "0", "default_resolved"), ("qsv.adaptive_b", "-1", "default_resolved"),
                                                 ("qsv.b_strategy", "0", "identity"), ("qsv.preset", "4", "identity"), ("qsv.q", "30", "identity")])
        c = body["cells"][0]
        self.assertEqual(c["argv"][:len(PLUMBING)], PLUMBING)
        self.assertEqual(c["argv"][c["argv"].index("-c:v"):], ["-c:v", "av1_qsv", "-q:v", "30", "-b_strategy", "0", "-preset", "4", "-an", "-sn",
                                                                f"/mnt/data/sweep/runs/{plan.run_id}/enc/{cell.cell_key}.mkv"])
        self.assertEqual((c["keep"], c["cut_kind"], c["window_id"]), (True, "reference", "tng"))
        tng = next(i for i in body["inputs"] if i["window_id"] == "tng" and i["kind"] == "reference")
        self.assertEqual((tng["path"], tng["content_sha"], tng["frames"], tng["probe_argv"]),
                         ("/mnt/data/sweep/refsets/stage-1080p/tng.reference.mkv", "sha-tng-ref", 1439, None))
        self.assertEqual(body["run"]["device"], "/dev/dri/by-path/pci-0000:03:00.0-render")

    def test_viewing_refuses_an_unknown_setting_and_a_window_outside_the_class(self):
        with self.assertRaises(Refusal) as cm:
            self.viewing([{"window_id": "tng", "settings": {"qsv.nope": "1"}}])
        self.assertIn("qsv.nope", str(cm.exception))
        with self.assertRaises(Refusal) as cm:
            self.viewing([{"window_id": "sopranos", "settings": {"qsv.q": "30", "qsv.preset": "4", "qsv.b_strategy": "0"}}])
        self.assertIn("sopranos", str(cm.exception))

    # ---- encode
    def test_encode_plans_the_base_arm_on_every_in_range_rung_on_every_member_plus_candidates_and_the_incumbent(self):
        plan, body = self.plan_in(planner.plan_encode, SEARCH, "media-01", NOW)
        self.assertEqual((plan.stage, plan.encoder_unit_id, plan.content_class_id, plan.search_id, plan.node_label),
                         ("encode", B580, CLASS, SEARCH, f"media-01:{B580}"))
        self.assertEqual(plan.windows, ("mrrobot", "parks", "shield", "snowpiercer", "tng", "tos"))
        self.assertEqual(len(plan.cells), 100)                                   # 13 in-range rungs x 6 members, 2 candidates x 2 windows x 4 rungs, the incumbent x 6
        self.assertEqual(len({c.cell_key for c in plan.cells}), 100)
        base = [c for c in plan.cells if ("qsv.preset", "4") in identity_settings(c) and ("qsv.b_strategy", "0") in identity_settings(c)]
        self.assertEqual(len(base), 78)
        incumbent = [c for c in plan.cells if identity_settings(c) == [("qsv.b_strategy", "1"), ("qsv.preset", "1"), ("qsv.q", "30")]]
        self.assertEqual(sorted(c.window_id for c in incumbent), sorted(plan.windows))
        candidates = [c for c in plan.cells if c not in base and c not in incumbent]
        self.assertEqual((len(candidates), sorted({c.window_id for c in candidates})), (16, ["parks", "tng"]))
        a = base[0]
        self.assertEqual(a.cell_key, recipes.cell_key(B580, f"sha-{a.window_id}-ref", "reference", a.window_id, identity_settings(a), FFMPEG))
        self.assertEqual(len(body["cells"]), 100)
        self.assertTrue(body["cells"][0]["keep"])                                # an encode-stage encode is kept until it is scored
        self.assertEqual(body["skipped"], [])

    def test_encode_filters_must_exist_and_a_subset_is_the_subset(self):
        plan, _ = self.plan_in(planner.plan_encode, SEARCH, "media-01", NOW, windows=["tng"], rungs=[24, 30], arms=["arm-a"])
        self.assertEqual(sorted(identity_settings(c) for c in plan.cells), [[("qsv.b_strategy", "0"), ("qsv.preset", "4"), ("qsv.q", "24")],
                                                                            [("qsv.b_strategy", "0"), ("qsv.preset", "4"), ("qsv.q", "30")]])
        for kw, word in ((dict(windows=["sopranos"]), "sopranos"), (dict(rungs=[99]), "99"), (dict(arms=["arm-z"]), "arm-z")):
            with self.assertRaises(Refusal) as cm:
                self.plan_in(planner.plan_encode, SEARCH, "media-01", NOW, **kw)
            self.assertIn(word, str(cm.exception))

    def test_encode_skips_existing_keys_and_refuses_when_nothing_is_left(self):
        first, body = self.plan_in(planner.plan_encode, SEARCH, "media-01", NOW, windows=["tng"], rungs=[24, 30], arms=["arm-a"])
        planner.enqueue(self.store, self.queue, first, body)
        later = NOW + dt.timedelta(minutes=1)
        second, body2 = self.plan_in(planner.plan_encode, SEARCH, "media-01", later)
        self.assertEqual(len(second.cells), 98)
        self.assertEqual(sorted(body2["skipped"]), sorted(c.cell_key for c in first.cells))
        planner.enqueue(self.store, self.queue, second, body2)
        with self.assertRaises(Refusal) as cm:
            self.plan_in(planner.plan_encode, SEARCH, "media-01", later + dt.timedelta(minutes=1))
        self.assertEqual(str(cm.exception), "REFUSING: nothing to encode: every cell of b580-qsv-av1 on media-01 exists already -- a cell is planned once; abandon and re-plan only what failed")

    # ---- enqueue, done, claim
    def test_enqueue_writes_the_rows_then_the_entry(self):
        plan, body = self.viewing()
        entry_id = planner.enqueue(self.store, self.queue, plan, body)
        run = next(r for r in self.store.rows("run") if r["run_id"] == plan.run_id)
        self.assertEqual((run["state"], run["stage"], run["host"]), ("planned", "viewing", "media-01"))
        self.assertEqual([e["state"] for e in self.store.rows("run_event") if e["run_id"] == plan.run_id], ["planned"])
        pending = self.queue.pending("media-01")
        self.assertEqual((len(pending), pending[0].entry_id, pending[0].run_id, pending[0].plan["run"]["run_id"]), (1, entry_id, plan.run_id, plan.run_id))
        self.assertEqual(self.store.check(), {})

    def test_enqueue_refuses_a_blocked_host_before_the_entry(self):
        plan, body = self.viewing()
        self.store.conn.execute("UPDATE host SET blocked = 'fix the mount' WHERE host = 'media-01'")
        with self.assertRaises(Refusal) as cm:
            planner.enqueue(self.store, self.queue, plan, body)
        self.assertIn("x_run_on_a_blocked_host", str(cm.exception))
        self.assertEqual(self.queue.pending("media-01"), [])
        self.assertNotIn(plan.run_id, [r["run_id"] for r in self.store.rows("run")])

    def test_done_cells_and_the_claim_body(self):
        plan, body = self.viewing()
        planner.enqueue(self.store, self.queue, plan, body)
        first = plan.cells[0].cell_key
        with self.store.transaction() as conn:
            ingest.ingest_record(conn, plan.run_id, ingest.parse_record({"kind": "encode", "cell_key": first, "bytes": 52000000, "bitrate_kbps": 6900.0,
                                                                          "frames": 1439, "duration_s": 60.0, "decode_path": "software", "kept": True}))
        entry = self.queue.claim("media-01")
        with self.store.reading() as conn:
            claim = planner.claim_body(conn, entry)
        self.assertEqual(claim["done"], [first])
        self.assertEqual(len(claim["cells"]), 2)
        with self.store.reading() as conn:
            inv, _ = planner.plan_inventory(conn, "media-01", "movies", [{"title_id": "mrrobot", "path": "/media/x.mkv"}], NOW)
            self.assertEqual(planner.done_cells(conn, {"run_id": "b580-inventory", "stage": "inventory", "parent_run_id": None},
                                                {"inputs": [{"title_id": "mrrobot", "path": "/mnt/storage/mrrobot.mkv"}, {"title_id": "nope", "path": "/x"}]}),
                             {"mrrobot"})


class ScoreTimePublish(unittest.TestCase):
    def setUp(self):
        self.store = fixture_store()
        self.addCleanup(self.store.close)
        self.queue = FakeQueue()

    def plan_in(self, fn, *args, **kw):
        with self.store.reading() as conn:
            return fn(conn, *args, **kw)

    def add_scorer(self, host):
        self.store.conn.execute("INSERT INTO scorer VALUES (?, '[\"/usr/local/bin/FFVship\"]', '[\"/opt/jellyfin-ffmpeg/bin/ffmpeg\"]', 'libvmaf_cuda', 0, '/cache')", (host,))

    # ---- score
    def test_score_defaults_to_the_scorer_on_the_parents_machine_and_views_its_files(self):
        plan, body = self.plan_in(planner.plan_score, "b580-viewing", NOW)
        self.assertEqual((plan.stage, plan.host, plan.parent_run_id, plan.node_label), ("score", "media-01-score", "b580-viewing", "media-01-score"))
        self.assertEqual((plan.scorer_build, plan.ffvship_version, plan.metric_backend, plan.artifact),
                         ("FFVship 1.3 + 8.1.2-Jellyfin-0b0ea2d", "1.3", "libvmaf_cuda", "node-score:0.0.2.dev0+g0"))
        self.assertEqual(body["score"]["height"], 1548)                          # a search-less run: the served lane's
        self.assertEqual((body["score"]["w"], body["score"]["h"], body["score"]["keep"], body["score"]["metric_backend"]), (2752, 1548, False, "libvmaf_cuda"))
        self.assertEqual(body["score"]["tools"], {"ffvship": ["/usr/local/bin/FFVship"], "score_ffmpeg": ["/opt/jellyfin-ffmpeg/bin/ffmpeg"], "gpu_id": 0})
        self.assertEqual(sorted(c["cell_key"] for c in body["cells"]), ["g-a", "g-b", "g-i"])
        ga = next(c for c in body["cells"] if c["cell_key"] == "g-a")
        self.assertEqual(ga["encode"], {"path": "/mnt/data/sweep/runs/b580-viewing/enc/g-a.mkv", "pull": None})
        self.assertEqual(ga["reference"], {"path": "/mnt/data/sweep/refsets/stage-1080p/tng.reference.mkv", "pull": None, "content_sha": "sha-tng-ref"})
        self.assertEqual(plan.windows, ("tng",))

    def test_score_refuses_a_host_without_a_scorer_a_build_without_the_filter_and_a_silent_agent(self):
        with self.assertRaises(Refusal) as cm:
            self.plan_in(planner.plan_score, "b580-viewing", NOW, scorer="eta")
        self.assertEqual(str(cm.exception), "REFUSING: eta has no scorer row -- add-scorer for it, or score on a host that has one")
        self.store.conn.execute("INSERT INTO host_identity VALUES ('media-01-score','2026-09-08T09:00','node-score:0.0.2.dev1+g1','g1','8.1.2-Jellyfin','0b0ea2d','[\"scale\",\"libvmaf\"]','1.3',1)")
        with self.assertRaises(Refusal) as cm:
            self.plan_in(planner.plan_score, "b580-viewing", NOW)
        self.assertEqual(str(cm.exception), "REFUSING: media-01-score's ffmpeg build lacks libvmaf_cuda, the scorer's metric_backend -- score with the node-score image, or add-scorer with --metric-backend libvmaf")
        self.add_scorer("htpc-01")
        with self.assertRaises(Refusal) as cm:
            self.plan_in(planner.plan_score, "b580-viewing", NOW, scorer="htpc-01")
        self.assertIn("never reported an identity", str(cm.exception))

    def test_score_on_another_machine_pulls_published_files_and_refuses_unpublished(self):
        self.add_scorer("eta-wsl")
        with self.assertRaises(Refusal) as cm:
            self.plan_in(planner.plan_score, "b580-viewing", NOW, scorer="eta-wsl")
        self.assertEqual(str(cm.exception), "REFUSING: reference cut tng.ref is neither on eta nor on the share -- publish --cut tng.ref first, or score on media-01-score")
        with self.store.transaction() as conn:
            exchange.record_publish(conn, "refsets/stage-1080p/tng.reference.mkv", "media-01", 2000000000, "r" * 64, "2026-09-08T09:00", cut_id="tng.ref")
        with self.assertRaises(Refusal) as cm:
            self.plan_in(planner.plan_score, "b580-viewing", NOW, scorer="eta-wsl")
        self.assertEqual(str(cm.exception), "REFUSING: cell g-a of run b580-viewing is not on the share -- publish --run b580-viewing first, or score on media-01-score")
        with self.store.transaction() as conn:
            for key in ("g-a", "g-b", "g-i"):
                exchange.record_publish(conn, f"runs/b580-viewing/enc/{key}.mkv", "media-01", 52000000, "e" * 64, "2026-09-08T09:10", run_id="b580-viewing", cell_key=key)
        plan, body = self.plan_in(planner.plan_score, "b580-viewing", NOW, scorer="eta-wsl", keep=True)
        self.assertEqual(plan.host, "eta-wsl")
        ga = next(c for c in body["cells"] if c["cell_key"] == "g-a")
        self.assertEqual(ga["encode"], {"path": "/home/sweep/work/runs/b580-viewing/enc/g-a.mkv",
                                        "pull": {"relative": "runs/b580-viewing/enc/g-a.mkv", "sha256": "e" * 64, "bytes": 52000000}})
        self.assertEqual(ga["reference"]["pull"]["relative"], "refsets/stage-1080p/tng.reference.mkv")
        self.assertTrue(body["score"]["keep"])

    def test_score_refuses_a_run_with_nothing_kept(self):
        with self.assertRaises(Refusal) as cm:
            self.plan_in(planner.plan_score, "b580-qsv-av1", NOW)                # every encode there was scored and discarded
        self.assertIn("kept", str(cm.exception))

    # ---- time
    def test_time_plans_the_source_cut_through_the_chain_per_configuration(self):
        plan, body = self.plan_in(planner.plan_time, "b580-viewing", NOW)          # the class serves one lane, so --lane is implied
        self.assertEqual((plan.stage, plan.host, plan.encoder_unit_id, plan.content_class_id, plan.parent_run_id, plan.windows), ("time", "media-01", B580, CLASS, None, ("tng",)))
        self.assertEqual(len(plan.cells), 3)                                       # three configurations viewed on one window
        for c, spec in zip(plan.cells, body["cells"]):
            self.assertEqual(c.cut_kind, "source")
            self.assertEqual(c.cell_key, recipes.cell_key(B580, "sha-tng-src", "source", "tng", identity_settings(c), FFMPEG))
            self.assertEqual(spec["argv"][len(PLUMBING):len(PLUMBING) + 10], ["-hwaccel", "qsv", "-hwaccel_output_format", "qsv", "-qsv_device", "/dev/dri/by-path/pci-0000:03:00.0-render",
                                                                               "-i", "/mnt/data/sweep/refsets/stage-1080p/tng.source.mkv", "-vf", "null"])
            self.assertEqual((spec["repeats"], spec["workers"], spec["legs"], spec["keep"]), (5, 1, ["full"], False))
        src = next(i for i in body["inputs"] if i["kind"] == "source")
        self.assertEqual(src["probe_argv"][-4:], ["-frames:v", "1", "-f", "null", "-"][-4:])
        self.assertEqual(body["run"]["chain"], {"lane": LANE, "vf_template": "null"})

    def test_time_needs_a_chain_and_a_lane_when_the_class_serves_two(self):
        with self.assertRaises(Refusal) as cm:
            self.plan_in(planner.plan_time, "b580-viewing", NOW, lane="m4-ipad-gt1080p-sdr")
        self.assertIn("chain", str(cm.exception))
        self.store.conn.execute("INSERT INTO content_class_lane VALUES ('native-1080p-sdr', 'kids-ipad-standard-sdr')")
        with self.assertRaises(Refusal) as cm:
            self.plan_in(planner.plan_time, "b580-viewing", NOW)
        self.assertEqual(str(cm.exception), "REFUSING: native-1080p-sdr serves kids-ipad-standard-sdr, m4-ipad-le1080p-sdr -- time --lane names which chain to time")

    def test_time_refuses_when_the_machine_is_not_quiet(self):
        with self.store.transaction() as conn:
            st.post_event(conn, "b580-qsv-av1-screen", "2026-09-08T09:00", "running", "first cell started", by="agent")
        plan, body = self.plan_in(planner.plan_time, "b580-viewing", NOW)
        with self.assertRaises(Refusal) as cm:
            planner.enqueue(self.store, self.queue, plan, body)
        self.assertEqual(str(cm.exception), "REFUSING: machine media-01 is not quiet: b580-qsv-av1-screen is running on media-01 -- wait for it or abandon it; a timing run runs alone on its machine")
        self.assertEqual(self.queue.pending("media-01"), [])

    # ---- publish
    def test_publish_lists_a_runs_kept_encodes_and_a_cut_via_another_runtime(self):
        host, body = self.plan_in(planner.plan_publish, run_id="b580-viewing")
        self.assertEqual(host, "media-01")
        self.assertEqual(body["files"][0], {"local": "/mnt/data/sweep/runs/b580-viewing/enc/g-a.mkv", "relative": "runs/b580-viewing/enc/g-a.mkv",
                                            "run_id": "b580-viewing", "cell_key": "g-a", "cut_id": None})
        self.assertEqual(len(body["files"]), 3)
        host, body = self.plan_in(planner.plan_publish, run_id="b580-viewing", via="media-01-score")
        self.assertEqual((host, body["files"][0]["local"]), ("media-01-score", "/mnt/data/sweep/runs/b580-viewing/enc/g-a.mkv"))
        host, body = self.plan_in(planner.plan_publish, cut_ids=["tng.ref"], via="media-01-score")
        self.assertEqual(body["files"], [{"local": "/mnt/data/sweep/refsets/stage-1080p/tng.reference.mkv", "relative": "refsets/stage-1080p/tng.reference.mkv",
                                          "run_id": None, "cell_key": None, "cut_id": "tng.ref"}])
        with self.assertRaises(Refusal) as cm:
            self.plan_in(planner.plan_publish, cut_ids=["tng.ref"], via="eta-wsl")   # nothing on eta's machine holds the reference set
        self.assertIn("tng.ref", str(cm.exception))
        with self.assertRaises(Refusal):
            self.plan_in(planner.plan_publish, run_id="b580-qsv-av1")                 # nothing kept


if __name__ == "__main__":
    unittest.main()
