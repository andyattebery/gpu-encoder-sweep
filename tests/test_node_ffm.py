#!/usr/bin/env python3
"""sweep/node: the tool layer the agent runs on -- the -progress parser that demands progress=end, the framemd5 content
hash, the decode probe read from stderr and never the exit status, the tool versions, a bare Windows path taken whole,
atomic records never overwritten, a pool that raises the first error in submission order, the config, the identity.

    python3 -m unittest tests.test_node_ffm
"""
import json
import os
import pathlib
import tempfile
import unittest
from unittest import mock

from sweep.node import config as node_config, ffm, identity, pool, records

PROGRESS = "frame=100\nfps=0.00\ntotal_size=5000\nout_time_us=4000000\nspeed=2.5x\nprogress=continue\nframe=1439\ntotal_size=60000000\nout_time_us=60000000\nspeed=24.0x\nprogress=end\n"


class Progress(unittest.TestCase):
    def test_the_last_block_wins_and_end_is_required(self):
        self.assertEqual(ffm.parse_progress(PROGRESS), {"frames": 1439, "size_bytes": 60000000, "out_time_s": 60.0, "speed": 24.0})
        with self.assertRaises(ffm.ParseError):
            ffm.parse_progress(PROGRESS.replace("progress=end", "progress=continue"))
        with self.assertRaises(ffm.ParseError):
            ffm.parse_progress("")

    def test_a_null_leg_reports_no_size_so_only_the_frame_count_is_read(self):
        leg = PROGRESS.replace("total_size=60000000", "total_size=N/A")
        with self.assertRaises(ffm.ParseError):
            ffm.parse_progress(leg)
        self.assertEqual(ffm.progress_frames(leg), 1439)
        self.assertEqual(ffm.progress_frames(""), 0)


class Tools(unittest.TestCase):
    def test_a_bare_path_is_one_argument_even_with_spaces(self):
        d = pathlib.Path(tempfile.mkdtemp()) / "Program Files (x)"
        d.mkdir()
        exe = d / "ffmpeg"
        exe.write_text("")
        self.assertEqual(ffm.as_cmd(str(exe)), [str(exe)])
        self.assertEqual(ffm.as_cmd("docker exec sweepbox /ff/bin/ffmpeg"), ["docker", "exec", "sweepbox", "/ff/bin/ffmpeg"])
        with mock.patch.object(ffm.os, "name", "nt"):
            self.assertEqual(ffm.as_cmd(r'"c:\Program Files\jellyfin-ffmpeg\bin\ffmpeg.exe" -hide_banner'), [r"c:\Program Files\jellyfin-ffmpeg\bin\ffmpeg.exe", "-hide_banner"])

    def test_the_decode_path_is_read_from_stderr_never_the_exit_status(self):
        self.assertEqual(ffm.decode_path_of("[vc1] No support for codec vc1 profile 3.\n[vc1] Failed setup for format vaapi: ...", 0), "software")
        self.assertEqual(ffm.decode_path_of("Stream mapping ... (h264 (native) -> wrapped_avframe)\n", 0), "hardware")
        with self.assertRaises(ffm.ToolError) as cm:
            ffm.decode_path_of("Unrecognized option 'qsv_device'", 1)                  # the probe did not run: not a capability answer
        self.assertIn("not a capability answer", str(cm.exception))

    def test_the_content_hash_is_of_the_frames_and_counts_them(self):
        out = "#format: frame checksums\n#version: 2\n0,          0,          0,        1,  3110400, 3f1e2d\n0,          1,          1,        1,  3110400, 9a8b7c\n"
        sha, frames = ffm.content_hash_of(out)
        self.assertEqual((len(sha), frames), (32, 2))
        self.assertEqual(sha, ffm.content_hash_of(out.replace("#version: 2", "#version: 3"))[0])       # comments are not content
        with self.assertRaises(ffm.ParseError):
            ffm.content_hash_of("#only comments\n")

    def test_versions_and_filters_are_parsed_from_the_tools_own_output(self):
        self.assertEqual(ffm.build_of("ffmpeg version 8.1.2-Jellyfin Copyright (c) 2000-2026 the FFmpeg developers\nbuilt with gcc"), "8.1.2-Jellyfin")
        filters = "Filters:\n  T.. = Timeline support\n TSC scale             V->V       Scale the input video size.\n ... libvmaf_cuda      VV->V      Calculate the VMAF (CUDA).\n"
        self.assertEqual(ffm.filters_of(filters), ["libvmaf_cuda", "scale"])
        self.assertEqual(ffm.ffvship_version_of("FFVship v5.1.0 (CUDA)\n"), "5.1.0")
        with self.assertRaises(ffm.ParseError):
            ffm.build_of("something else")


class Records(unittest.TestCase):
    def test_a_record_is_written_atomically_and_never_overwritten(self):
        d = pathlib.Path(tempfile.mkdtemp())
        records.write_record(d, "k1", {"kind": "encode", "cell_key": "k1"})
        records.write_record(d, "k1", {"kind": "encode", "cell_key": "k1"})                                # identical: a no-op
        with self.assertRaises(records.RecordExists):
            records.write_record(d, "k1", {"kind": "encode", "cell_key": "k1", "bytes": 1})
        records.write_record(d, "k2", {"kind": "failure", "cell_key": "k2"})
        self.assertEqual(sorted(r["cell_key"] for r in records.read_records(d)), ["k1", "k2"])
        self.assertEqual(sorted(p.name for p in d.iterdir()), ["k1.json", "k2.json"])


class Pool(unittest.TestCase):
    def test_every_future_is_joined_and_the_first_error_in_submission_order_is_raised(self):
        self.assertEqual(pool.run_all([lambda: 1, lambda: 2, lambda: 3], workers=2), [1, 2, 3])

        def boom(n):
            raise RuntimeError(f"job {n}")
        with self.assertRaises(RuntimeError) as cm:
            pool.run_all([lambda: 0, lambda: boom(1), lambda: boom(2)], workers=3)
        self.assertEqual(str(cm.exception), "job 1")


class Config(unittest.TestCase):
    def test_three_variables_and_nothing_else(self):
        c = node_config.Config.from_env({"SWEEP_HUB": "http://hub:8000", "SWEEP_TOKEN": "t", "SWEEP_HOST": "media-01"})
        self.assertEqual((c.hub, c.token, c.host, c.artifact_override), ("http://hub:8000", "t", "media-01", None))
        with self.assertRaises(SystemExit) as cm:
            node_config.Config.from_env({"SWEEP_HUB": "http://hub:8000", "SWEEP_TOKEN": "t"})
        self.assertEqual(str(cm.exception), "REFUSING: SWEEP_HOST is not set -- the role writes it: the host row this agent acts for")


class Identity(unittest.TestCase):
    HOST = {"host": "media-01", "work_root": "/tmp", "ffmpeg": "/opt/jellyfin-ffmpeg/bin/ffmpeg", "os": "linux"}

    def identify(self, env, stamp=None, scorer=None):
        c = node_config.Config.from_env({"SWEEP_HUB": "h", "SWEEP_TOKEN": "t", "SWEEP_HOST": "media-01", **env})
        with mock.patch.object(ffm, "tool_versions", return_value=("8.1.2-Jellyfin", "0b0ea2d", ["scale", "libvmaf_cuda"])), \
             mock.patch.object(ffm, "ffvship_version", return_value="5.1.0"), \
             mock.patch.object(ffm, "free_bytes", return_value=123), \
             mock.patch.object(identity, "package_version", return_value=env.pop("_version", "0.0.3.dev5+gabc1234")):
            return identity.identify(c, self.HOST, scorer, stamp_path=stamp)

    def test_the_artifact_is_the_override_the_stamp_or_uvx_with_the_version(self):
        self.assertEqual(self.identify({"SWEEP_ARTIFACT": "node-encode:9.9.9"})["artifact"], "node-encode:9.9.9")
        stamp = pathlib.Path(tempfile.mkdtemp()) / "sweep-artifact"
        stamp.write_text("node-encode:0.0.3.dev5+gabc1234\nabc1234def\n")
        got = self.identify({}, stamp=stamp)
        self.assertEqual((got["artifact"], got["harness_version"]), ("node-encode:0.0.3.dev5+gabc1234", "abc1234def"))
        got = self.identify({})
        self.assertEqual((got["artifact"], got["harness_version"]), ("uvx:0.0.3.dev5+gabc1234", "gabc1234"))
        self.assertEqual(self.identify({"_version": "1.0.0"})["harness_version"], "1.0.0")           # a clean tag names its commit

    def test_the_tools_are_reported_and_ffvship_only_with_a_scorer(self):
        got = self.identify({})
        self.assertEqual((got["ffmpeg_build"], got["ffmpeg_sha"], got["ffmpeg_filters"], got["ffvship_version"], got["free_bytes"]),
                         ("8.1.2-Jellyfin", "0b0ea2d", ["scale", "libvmaf_cuda"], None, 123))
        self.assertEqual(self.identify({}, scorer={"ffvship": ["/usr/local/bin/FFVship"]})["ffvship_version"], "5.1.0")


if __name__ == "__main__":
    unittest.main()
