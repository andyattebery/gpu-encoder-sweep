#!/usr/bin/env python3
"""sweep/hub/build.py: the one command builder -- a cell's settings and the frontend's plumbing become argv in the hub;
the agent prepends the binary and composes nothing. The shapes are the archived harness's (sweep.py:690-874).

    python3 -m unittest tests.test_hub_build
"""
import unittest

from sweep.hub import build
from tests.hub_helpers import fixture_store

B580_DEVICE = "/dev/dri/by-path/pci-0000:03:00.0-render"
PLUMBING = ["-nostdin", "-y", "-hide_banner", "-loglevel", "error", "-nostats"]


class OrderedFlags(unittest.TestCase):
    def setUp(self):
        self.store = fixture_store()
        self.addCleanup(self.store.close)

    def test_e1_order_mode_selectors_then_the_anchor_then_the_rest_by_id(self):
        # given in the wrong order on purpose; roles are carried through, computed and default_resolved included
        settings = [("qsv.preset", "4", "identity"), ("qsv.adaptive_b", "-1", "default_resolved"), ("qsv.b_strategy", "0", "identity"), ("qsv.q", "24", "identity")]
        with self.store.reading() as conn:
            self.assertEqual(build.ordered_flags(conn, settings), [("-q:v", "24"), ("-adaptive_b", "-1"), ("-b_strategy", "0"), ("-preset", "4")])
            nvenc = [("nvenc.preset", "p2", "identity"), ("nvenc.cq", "20", "identity"), ("nvenc.rc", "vbr", "identity"), ("nvenc.tune", "uhq", "identity")]
            self.assertEqual(build.ordered_flags(conn, nvenc), [("-rc", "vbr"), ("-cq", "20"), ("-preset", "p2"), ("-tune", "uhq")])

    def test_an_unknown_setting_is_refused_by_name(self):
        with self.store.reading() as conn:
            with self.assertRaises(Exception) as cm:
                build.ordered_flags(conn, [("qsv.nope", "1", "identity")])
        self.assertIn("qsv.nope", str(cm.exception))


class EncodeArgv(unittest.TestCase):
    FLAGS = [("-q:v", "24"), ("-adaptive_b", "-1"), ("-b_strategy", "0"), ("-preset", "4")]

    def test_qsv_from_the_reference_cut(self):
        argv = build.encode_argv("qsv", "av1", self.FLAGS, B580_DEVICE, "/w/refsets/s/tng.reference.mkv", "/w/runs/r/enc/k.mkv")
        self.assertEqual(argv, PLUMBING + ["-init_hw_device", f"qsv=hw,child_device={B580_DEVICE}", "-filter_hw_device", "hw",
                                           "-i", "/w/refsets/s/tng.reference.mkv", "-vf", "format=p010,hwupload=extra_hw_frames=64",
                                           "-c:v", "av1_qsv", "-q:v", "24", "-adaptive_b", "-1", "-b_strategy", "0", "-preset", "4",
                                           "-an", "-sn", "/w/runs/r/enc/k.mkv"])

    def test_nvenc_from_the_reference_cut_pins_the_pixel_format(self):
        argv = build.encode_argv("nvenc", "av1", [("-rc", "vbr"), ("-cq", "20"), ("-preset", "p2")], "pci-0000:01:00.0", "in.mkv", "out.mkv")
        self.assertEqual(argv, PLUMBING + ["-i", "in.mkv", "-c:v", "av1_nvenc", "-rc", "vbr", "-cq", "20", "-preset", "p2",
                                           "-pix_fmt", "p010le", "-an", "-sn", "out.mkv"])

    def test_vaapi_from_the_reference_cut_and_hevc_tags_hvc1(self):
        argv = build.encode_argv("vaapi", "hevc", [("-global_quality", "22")], "/dev/dri/by-path/pci-0000:03:00.0-render", "in.mkv", "out.mkv")
        self.assertEqual(argv, PLUMBING + ["-vaapi_device", "/dev/dri/by-path/pci-0000:03:00.0-render", "-i", "in.mkv", "-vf", "format=p010,hwupload",
                                           "-c:v", "hevc_vaapi", "-global_quality", "22", "-tag:v", "hvc1", "-an", "-sn", "out.mkv"])

    def test_production_path_decodes_in_hardware_and_runs_the_chain(self):
        argv = build.encode_argv("qsv", "av1", self.FLAGS, B580_DEVICE, "in.mkv", "out.mkv", vf_template="scale_qsv=w=1920:h=1080")
        self.assertEqual(argv[len(PLUMBING):len(PLUMBING) + 6], ["-hwaccel", "qsv", "-hwaccel_output_format", "qsv", "-qsv_device", B580_DEVICE])
        self.assertEqual(argv[len(PLUMBING) + 6:len(PLUMBING) + 10], ["-i", "in.mkv", "-vf", "scale_qsv=w=1920:h=1080"])
        self.assertEqual(argv[-3:], ["-an", "-sn", "out.mkv"])

    def test_software_decode_bridge_drops_hwaccel_and_uploads(self):
        argv = build.encode_argv("vaapi", "hevc", [("-global_quality", "22")], "/dev/dri/renderD128", "in.mkv", "out.mkv",
                                 vf_template="scale_vaapi=w=1920:h=1080", software_decode=True)
        self.assertNotIn("-hwaccel", argv)
        self.assertIn("-vaapi_device", argv)
        self.assertEqual(argv[argv.index("-vf") + 1], "format=p010,hwupload,scale_vaapi=w=1920:h=1080")

    def test_an_unknown_frontend_is_refused(self):
        with self.assertRaises(ValueError):
            build.encode_argv("amf", "hevc", [], "d", "in", "out")


class OtherArgv(unittest.TestCase):
    def test_probe_is_one_frame_with_verbose_stderr_and_the_frontends_device_flag(self):
        self.assertEqual(build.probe_argv("qsv", B580_DEVICE, "in.mkv"),
                         ["-nostdin", "-hide_banner", "-loglevel", "verbose", "-nostats", "-hwaccel", "qsv", "-hwaccel_output_format", "qsv",
                          "-qsv_device", B580_DEVICE, "-i", "in.mkv", "-frames:v", "1", "-f", "null", "-"])
        self.assertEqual(build.hwaccel_flags("nvenc", "x"), ["-hwaccel", "cuda", "-hwaccel_output_format", "cuda"])
        self.assertEqual(build.hwaccel_flags("vaapi", "/dev/dri/renderD128"), ["-hwaccel", "vaapi", "-hwaccel_output_format", "vaapi", "-vaapi_device", "/dev/dri/renderD128"])

    def test_hash_and_rescale_argv(self):
        self.assertEqual(build.hash_argv("in.mkv"), ["-nostdin", "-hide_banner", "-loglevel", "error", "-i", "in.mkv", "-f", "framemd5", "-"])
        self.assertEqual(build.rescale_argv("in.mkv", "out.mkv", 2752, 1548),
                         ["-nostdin", "-y", "-v", "error", "-max_frame_delay", "16", "-i", "in.mkv", "-vf", "scale=w=2752:h=1548",
                          "-c:v", "ffvhuff", "-pred", "plane", "-pix_fmt", "yuv420p10le", "out.mkv"])

    def test_ffvship_argv_uses_source_not_reference(self):
        self.assertEqual(build.ffvship_argv("ref.mkv", "enc.mkv", "SSIMULACRA2", "/t/o.json", 0),
                         ["--source", "ref.mkv", "--encoded", "enc.mkv", "-m", "SSIMULACRA2", "--json", "/t/o.json"])
        self.assertEqual(build.ffvship_argv("ref.mkv", "enc.mkv", "Butteraugli", "/t/o.json", 1)[-2:], ["--gpu-id", "1"])

    def test_libvmaf_argv_puts_the_encode_first_and_the_cuda_device_before_the_inputs(self):
        plain = build.libvmaf_argv("enc.mkv", "ref.mkv", "/t/v.json", cuda=False, threads=0)
        self.assertEqual(plain, PLUMBING + ["-i", "enc.mkv", "-i", "ref.mkv", "-lavfi",
                                            "[0:v][1:v]libvmaf=feature=name=cambi|name=psnr|name=float_ssim:log_fmt=json:log_path='/t/v.json'",
                                            "-f", "null", "-"])
        cuda = build.libvmaf_argv("enc.mkv", "ref.mkv", "/t/v.json", cuda=True, threads=12)
        self.assertEqual(cuda[len(PLUMBING):len(PLUMBING) + 4], ["-init_hw_device", "cuda=cu", "-filter_hw_device", "cu"])
        self.assertEqual(cuda[cuda.index("-lavfi") + 1],
                         "[0:v]format=yuv420p10le,hwupload_cuda[d];[1:v]format=yuv420p10le,hwupload_cuda[r];[d][r]libvmaf_cuda="
                         "feature=name=cambi|name=psnr|name=float_ssim:log_fmt=json:log_path='/t/v.json':n_threads=12")

    def test_legs_are_prefixes_of_the_production_argv(self):
        argv = build.encode_argv("qsv", "av1", [("-q:v", "24")], B580_DEVICE, "in.mkv", "out.mkv", vf_template="scale_qsv=w=1920:h=1080")
        self.assertEqual(build.leg(argv, "full"), argv)
        decode = build.leg(argv, "decode")
        self.assertEqual(decode[-3:], ["-f", "null", "-"])
        self.assertNotIn("-vf", decode)
        self.assertNotIn("-c:v", decode)
        filters = build.leg(argv, "decode_filters")
        self.assertIn("-vf", filters)
        self.assertNotIn("-c:v", filters)
        with self.assertRaises(ValueError):
            build.leg(argv, "encode")


if __name__ == "__main__":
    unittest.main()
