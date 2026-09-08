#!/usr/bin/env python3
"""sweep/hub/artifact.py: what each agent reported, recorded once per change, and what a plan pins from it.

    python3 -m unittest tests.test_hub_artifact
"""
import unittest

from sweep.hub import artifact
from sweep.hub.refusals import Refusal
from tests.hub_helpers import fixture_store

NODE = artifact.Identity(artifact="node-encode:0.0.3.dev2+gabc1234", harness_version="gabc1234", ffmpeg_build="8.1.2-Jellyfin",
                         ffmpeg_sha="0b0ea2d", ffmpeg_filters=("scale", "format", "hwupload", "scale_cuda"), ffvship_version=None,
                         free_bytes=800000000000)
SCORER = artifact.Identity(artifact="node-score:0.0.3.dev2+gabc1234", harness_version="gabc1234", ffmpeg_build="8.1.2-Jellyfin",
                           ffmpeg_sha="0b0ea2d", ffmpeg_filters=("scale", "hwupload_cuda", "libvmaf", "libvmaf_cuda"), ffvship_version="5.1.0",
                           free_bytes=300000000000)


class Identities(unittest.TestCase):
    def setUp(self):
        self.store = fixture_store()
        self.addCleanup(self.store.close)

    def test_first_report_writes_and_is_current(self):
        with self.store.transaction() as conn:
            self.assertTrue(artifact.record_identity(conn, "htpc-01", NODE, "2026-09-06T08:00"))
            self.assertEqual(artifact.current_identity(conn, "htpc-01"), NODE)
        self.assertEqual(len([r for r in self.store.rows("host_identity") if r["host"] == "htpc-01"]), 1)

    def test_an_identical_report_is_a_noop_even_with_other_free_bytes(self):
        with self.store.transaction() as conn:
            artifact.record_identity(conn, "htpc-01", NODE, "2026-09-06T08:00")
            self.assertFalse(artifact.record_identity(conn, "htpc-01", NODE._replace(free_bytes=1), "2026-09-06T08:30"))
        rows = [r for r in self.store.rows("host_identity") if r["host"] == "htpc-01"]
        self.assertEqual([r["reported_at"] for r in rows], ["2026-09-06T08:00"])

    def test_a_changed_artifact_writes_a_second_row_and_becomes_current(self):
        newer = NODE._replace(artifact="node-encode:0.0.3.dev5+gdef5678", harness_version="gdef5678")
        with self.store.transaction() as conn:
            artifact.record_identity(conn, "htpc-01", NODE, "2026-09-06T08:00")
            self.assertTrue(artifact.record_identity(conn, "htpc-01", newer, "2026-09-07T08:00"))
            self.assertEqual(artifact.current_identity(conn, "htpc-01"), newer)
        self.assertEqual(len([r for r in self.store.rows("host_identity") if r["host"] == "htpc-01"]), 2)

    def test_the_fixture_identities_read_back(self):
        # the fixture reports media-01 twice; the later row is current and its filters come back as a tuple
        with self.store.reading() as conn:
            current = artifact.current_identity(conn, "media-01")
            self.assertEqual((current.artifact, current.ffmpeg_filters[-1]), ("node-encode:0.0.2.dev0+g0", "scale_cuda"))
            self.assertIsNone(artifact.current_identity(conn, "htpc-01"))

    def test_pins_are_the_run_plans_fields(self):
        self.assertEqual(artifact.pins(NODE), {"artifact": "node-encode:0.0.3.dev2+gabc1234", "harness_version": "gabc1234",
                                               "ffmpeg_build": "8.1.2-Jellyfin", "ffmpeg_sha": "0b0ea2d"})

    def test_scorer_build_names_ffvship_and_the_ffmpeg_build_and_refuses_a_host_without_ffvship(self):
        self.assertEqual(artifact.scorer_build(SCORER), "FFVship 5.1.0 + 8.1.2-Jellyfin-0b0ea2d")
        with self.assertRaises(Refusal) as cm:
            artifact.scorer_build(NODE)
        self.assertEqual(str(cm.exception), "REFUSING: the identity reports no FFVship -- score on a host whose agent reports one: the node-score image")

    def test_a_report_for_a_host_the_store_lacks_is_refused_by_name(self):
        with self.assertRaises(Refusal) as cm:
            with self.store.transaction() as conn:
                artifact.record_identity(conn, "no-such-host", NODE, "2026-09-06T08:00")
        self.assertIn("host", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
