#!/usr/bin/env python3
"""sweep/node/jobs.py: what the agent does with one cell -- encode it and count its frames, probe a title, adopt a cut,
score by S1, time by T1, publish with a sha both ends -- against the fake tools under tests/fake_tools.

    python3 -m unittest tests.test_node_jobs
"""
import hashlib
import json
import os
import pathlib
import stat
import tempfile
import unittest
from unittest import mock

from sweep.hub import build
from sweep.node import jobs

TOOLS = pathlib.Path(__file__).parent / "fake_tools"


def ctx_for(root, share=None):
    work = pathlib.Path(root) / "work"
    (work / "refsets" / "rs").mkdir(parents=True)
    return jobs.Context(host="media-01", os="linux", work_root=str(work), share_root=None if share is None else str(share),
                        ffmpeg=[str(TOOLS / "ffmpeg")], ffprobe=[str(TOOLS / "ffprobe")], ffvship=[str(TOOLS / "FFVship")],
                        score_ffmpeg=[str(TOOLS / "ffmpeg")], run_dir=work / "runs" / "r1")


def cut_file(ctx, name="tng.reference.mkv", size=200000):
    path = pathlib.Path(ctx.work_root) / "refsets" / "rs" / name
    path.write_bytes(b"c" * size)
    return str(path)


class Encoding(unittest.TestCase):
    def setUp(self):
        self.root = pathlib.Path(tempfile.mkdtemp())
        self.ctx = ctx_for(self.root)
        self.src = cut_file(self.ctx)
        self.dst = str(self.ctx.run_dir / "enc" / "k1.mkv")
        argv = build.encode_argv("qsv", "av1", [("-q:v", "30")], "/dev/dri/renderD128", self.src, self.dst)
        self.cell = {"cell_key": "k1", "window_id": "tng", "cut_kind": "reference", "argv": argv, "output": self.dst, "keep": True, "repeats": 1, "workers": 1, "legs": ["full"]}
        self.inputs = {("tng", "reference"): {"cut_id": "tng.ref", "window_id": "tng", "kind": "reference", "path": self.src, "content_sha": "x", "frames": 1439, "probe_argv": None}}

    def test_an_encode_record_counts_frames_from_progress_and_bytes_from_disk(self):
        record = jobs.encode_cell(self.cell, self.inputs, self.ctx)
        self.assertEqual((record["kind"], record["cell_key"], record["frames"], record["kept"], record["decode_path"]), ("encode", "k1", 1439, True, "software"))
        self.assertEqual(record["bytes"], os.path.getsize(self.dst))
        self.assertAlmostEqual(record["bitrate_kbps"], record["bytes"] * 8 / 1000 / 60.0, places=2)
        self.assertEqual(record["duration_s"], 60.0)

    def test_short_of_frames_is_a_failure_record_with_the_stderr(self):
        with mock.patch.dict(os.environ, {"FAKE_FRAMES": "1000"}):
            record = jobs.encode_cell(self.cell, self.inputs, self.ctx)
        self.assertEqual((record["kind"], record["rc"]), ("failure", 0))
        self.assertTrue(record["stderr"].startswith("short of frames: 1000 of 1439"))
        with mock.patch.dict(os.environ, {"FAKE_RC": "218"}):
            record = jobs.encode_cell(self.cell, self.inputs, self.ctx)
        self.assertEqual((record["kind"], record["rc"]), ("failure", 218))
        self.assertIn("Error initializing the encoder", record["stderr"])

    def test_the_decode_path_comes_from_the_probe_when_the_plan_carries_one(self):
        self.inputs[("tng", "reference")]["probe_argv"] = build.probe_argv("qsv", "/dev/dri/renderD128", self.src)
        self.assertEqual(jobs.encode_cell(self.cell, self.inputs, self.ctx)["decode_path"], "hardware")
        with mock.patch.dict(os.environ, {"FAKE_PROBE": "[vc1] No support for codec vc1 profile 3.\n"}):
            self.assertEqual(jobs.encode_cell(self.cell, self.inputs, self.ctx)["decode_path"], "software")
        with mock.patch.dict(os.environ, {"FAKE_PROBE": "boom", "FAKE_PROBE_RC": "1"}):
            with self.assertRaises(jobs.JobError):
                jobs.encode_cell(self.cell, self.inputs, self.ctx)

    def test_timing_repeats_with_the_first_as_warm_up(self):
        cell = dict(self.cell, repeats=3, keep=False)
        encode, timing = jobs.time_cell(cell, self.inputs, self.ctx)
        self.assertEqual((encode["kind"], encode["kept"]), ("encode", False))
        self.assertEqual([(s["repeat_index"], s["is_warmup"], s["leg"], s["frames"], s["workers"]) for s in timing["samples"]],
                         [(0, True, "full", 1439, 1), (1, False, "full", 1439, 1), (2, False, "full", 1439, 1)])
        self.assertTrue(all(s["fps"] > 0 and s["wall_s"] > 0 for s in timing["samples"]))
        self.assertFalse(pathlib.Path(self.dst).exists())                                            # discarded


class Sample(unittest.TestCase):
    def setUp(self):
        self.root = pathlib.Path(tempfile.mkdtemp())
        self.ctx = ctx_for(self.root)

    def test_inventory_probes_the_title(self):
        title = self.root / "Show.S01E01.2019.1080p.WEB-DL.mkv"
        title.write_bytes(b"t")
        record = jobs.inventory_title({"title_id": "show-1", "path": str(title), "library": "tv"}, self.ctx)
        self.assertEqual((record["kind"], record["title_id"], record["library"], record["width"], record["height"], record["video_codec"], record["field_order"]),
                         ("title", "show-1", "tv", 1920, 1080, "h264", "progressive"))
        self.assertEqual((record["dynamic_range"], record["dv_profile"], record["bit_depth"], record["source_type"], record["audio_layout"], record["subtitle_layout"]),
                         ("sdr", None, 8, "web", "eac3 5.1", "subrip eng"))
        self.assertAlmostEqual(record["fps"], 23.976, places=3)
        self.assertEqual(record["bitrate_kbps"], 18500.0)
        self.assertAlmostEqual(record["bpp"], 18500000 / (1920 * 1080 * 23.976), places=4)
        with mock.patch.dict(os.environ, {"FAKE_TRANSFER": "smpte2084", "FAKE_DV": "1"}):
            record = jobs.inventory_title({"title_id": "x", "path": str(title), "library": "tv"}, self.ctx)
        self.assertEqual((record["dynamic_range"], record["dv_profile"]), ("dv", 8))

    def test_adopt_hashes_counts_and_copies_into_the_refset_home(self):
        original = self.root / "stage" / "tng.ref.mkv"
        original.parent.mkdir()
        original.write_bytes(b"r" * 100000)
        cut = {"cut_id": "tng.ref", "window_id": "tng", "kind": "reference", "path": str(original),
               "dest": str(pathlib.Path(self.ctx.work_root) / "refsets" / "rs" / "tng.reference.mkv"),
               "chain": {"lane": "l", "host": "media-01", "encoder_unit_id": "u"}}
        record = jobs.adopt_cut(cut, {"id": "rs", "geometry": "1920x1080", "pix_fmt": "p010le", "post": True}, self.ctx)
        self.assertEqual((record["kind"], record["cut_id"], record["reference_set_id"], record["cut_kind"], record["frames"], record["bytes"], len(record["content_sha"])),
                         ("cut", "tng.ref", "rs", "reference", 1439, 100000, 32))
        self.assertEqual((record["chain_lane"], record["chain_host"], record["chain_unit"]), ("l", "media-01", "u"))
        self.assertEqual(pathlib.Path(cut["dest"]).read_bytes(), b"r" * 100000)
        self.assertEqual(jobs.adopt_cut(cut, {"id": "rs", "post": False}, self.ctx)["content_sha"], record["content_sha"])   # already in place: no second copy
        pathlib.Path(cut["dest"]).write_bytes(b"z")
        with self.assertRaises(jobs.JobError):
            jobs.adopt_cut(cut, {"id": "rs", "post": False}, self.ctx)                                # a different file already holds the name


class Scoring(unittest.TestCase):
    def setUp(self):
        self.root = pathlib.Path(tempfile.mkdtemp())
        self.ctx = ctx_for(self.root)
        self.ref = cut_file(self.ctx)
        (self.ctx.run_dir / "enc").mkdir(parents=True)
        self.enc = self.ctx.run_dir / "enc" / "k1.mkv"
        self.enc.write_bytes(b"e" * 50000)
        self.score = {"height": 1548, "w": 2752, "h": 1548, "keep": False, "metric_backend": "libvmaf_cuda",
                      "tools": {"ffvship": self.ctx.ffvship, "score_ffmpeg": self.ctx.score_ffmpeg, "gpu_id": 0}, "cache_dir": str(self.root / "cache")}
        self.cell = {"cell_key": "k1", "window_id": "tng", "encode": {"path": str(self.enc), "pull": None},
                     "reference": {"path": self.ref, "pull": None, "content_sha": "sha-tng-ref"}}

    def test_s1_pools_by_nearest_rank_and_discards_the_encode(self):
        record, frames = jobs.score_cell(self.cell, self.score, self.ctx)
        scores = {(s["metric"], s["statistic"]): s["value"] for s in record["scores"]}
        self.assertEqual(record["recipe"], "S1")
        self.assertAlmostEqual(scores[("ssimulacra2", "mean")], 121591 / 1439, places=6)             # 80..89 cycling over 1439 frames
        self.assertEqual(scores[("ssimulacra2", "p5")], 80.0)
        self.assertEqual(scores[("ssimulacra2", "min")], 80.0)
        self.assertAlmostEqual(scores[("butteraugli", "max")], 2.6, places=9)                          # the infnorm's max
        self.assertEqual((scores[("vmaf", "mean")], scores[("cambi", "mean")], scores[("psnr_y", "mean")], scores[("float_ssim", "mean")]), (95.25, 0.31, 44.5, 0.985))
        self.assertEqual([s["scoring_step"] for s in record["steps"]], ["rescale_ref", "rescale_enc", "ssimu2", "butteraugli", "libvmaf"])
        self.assertEqual(sorted(frames), ["butteraugli", "ssimulacra2"])
        self.assertEqual(len(frames["ssimulacra2"]), 1439)
        self.assertFalse(self.enc.exists())
        self.assertFalse(record["kept"])

    def test_keep_keeps_the_encode_and_the_reference_is_rescaled_once_per_window(self):
        record, _ = jobs.score_cell(self.cell, dict(self.score, keep=True), self.ctx)
        self.assertTrue(self.enc.exists() and record["kept"])
        self.assertEqual(self.ctx.refcache.hits, 0)
        self.enc.write_bytes(b"f" * 50000)
        jobs.score_cell(dict(self.cell, cell_key="k2"), dict(self.score, keep=True), self.ctx)
        self.assertEqual(self.ctx.refcache.hits, 1)

    def test_a_pull_is_verified_by_sha_before_use(self):
        share = self.root / "share"
        (share / "runs" / "r0" / "enc").mkdir(parents=True)
        (share / "runs" / "r0" / "enc" / "k1.mkv").write_bytes(b"p" * 3000)
        ctx = ctx_for(self.root / "two", share=share)
        pulled = pathlib.Path(ctx.work_root) / "runs" / "r0" / "enc" / "k1.mkv"
        good = {"relative": "runs/r0/enc/k1.mkv", "sha256": hashlib.sha256(b"p" * 3000).hexdigest(), "bytes": 3000}
        self.assertEqual(jobs.pull(good, str(pulled), ctx), str(pulled))
        self.assertEqual(pulled.read_bytes(), b"p" * 3000)
        with self.assertRaises(jobs.JobError):
            jobs.pull(dict(good, sha256="0" * 64), str(pulled.with_name("k2.mkv")), ctx)


class Publishing(unittest.TestCase):
    def test_publish_copies_shas_both_ends_and_probes_the_share_first(self):
        root = pathlib.Path(tempfile.mkdtemp())
        share = root / "share"
        share.mkdir()
        ctx = ctx_for(root, share=share)
        local = root / "big.mkv"
        local.write_bytes(b"q" * 123456)
        spec = {"local": str(local), "relative": "runs/r1/enc/k1.mkv", "run_id": "r1", "cell_key": "k1", "cut_id": None}
        body = jobs.publish_file(spec, ctx)
        self.assertEqual((body["path"], body["by_host"], body["bytes"], body["run_id"], body["cell_key"], body["cut_id"]), ("runs/r1/enc/k1.mkv", "media-01", 123456, "r1", "k1", None))
        self.assertEqual(body["sha256"], hashlib.sha256(b"q" * 123456).hexdigest())
        self.assertEqual((share / "runs" / "r1" / "enc" / "k1.mkv").read_bytes(), b"q" * 123456)
        self.assertEqual(sorted(p.name for p in share.iterdir()), ["runs"])                             # the probe file is gone
        os.chmod(share, stat.S_IRUSR | stat.S_IXUSR)
        try:
            with self.assertRaises(jobs.JobError) as cm:
                jobs.publish_file(spec, ctx)
        finally:
            os.chmod(share, stat.S_IRWXU)
        self.assertIn("the share is not writable from media-01", str(cm.exception))
        self.assertIn("the role mounts temp/harness read-write", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
