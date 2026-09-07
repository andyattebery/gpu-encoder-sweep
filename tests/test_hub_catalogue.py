#!/usr/bin/env python3
"""sweep/hub/api/catalogue.py: every catalogue verb writes its FILE rows in one checked transaction, and every refusal
the schema or a check makes reaches the client as REFUSING.

    python3 -m unittest tests.test_hub_catalogue
"""
import json
import unittest

from sweep.hub import refusals
from sweep.hub.store import Store
from tests.hub_helpers import client_for, fixture_store, post

B580, A4000 = "intel-b580-ihd26.2.2-qsv-av1", "nvidia-a4000-595-nvenc-hevc"


class Catalogue(unittest.TestCase):
    def setUp(self):
        self.store = fixture_store()
        self.addCleanup(self.store.close)
        self.client = client_for(self.store)

    def rows(self, table, **key):
        return [r for r in self.store.rows(table) if all(r[k] == v for k, v in key.items())]

    def ok(self, verb, body):
        status, text = post(self.client, f"/catalogue/{verb}", body)
        self.assertEqual((status, text), (200, '{"ok":true}'))

    def refused(self, verb, body, starts, fix=None):
        status, text = post(self.client, f"/catalogue/{verb}", body)
        self.assertEqual(status, 422, text)
        self.assertTrue(text.startswith("REFUSING: " + starts), text)
        if fix is not None:
            self.assertTrue(text.endswith(" -- " + fix), text)
        return text

    # ---- add-host
    HOST = {"host": "htpc-02", "ssh_host": "htpc-02", "os": "linux", "machine": "htpc-02", "work_root": "/data/sweep",
            "share_root": "/mnt/nas-01/sweep", "ffmpeg": "/ffmpeg/ffmpeg"}

    def test_add_host_writes_the_row(self):
        self.ok("add-host", self.HOST)
        (row,) = self.rows("host", host="htpc-02")
        self.assertEqual((row["machine"], row["local_view"], row["blocked"]), ("htpc-02", None, None))

    def test_add_host_twice_is_refused(self):
        self.ok("add-host", self.HOST)
        self.refused("add-host", self.HOST, "host already has a row with that host", refusals.UNIQUE_FIXES["host"])

    def test_add_host_unknown_os_is_refused(self):
        self.refused("add-host", dict(self.HOST, os="plan9"), "os must be one of linux, windows", "give one of them")

    # ---- add-unit
    UNIT = {"encoder_unit_id": "intel-a380-ihd26.2.2-qsv-av1", "vendor": "intel", "card": "Arc A380", "driver": "iHD 26.2.2",
            "frontend": "qsv", "codec": "av1", "host": "media-01", "device": "pci-0000:04:00.0"}

    def test_add_unit_writes_the_unit_and_puts_it_on_the_host(self):
        self.ok("add-unit", self.UNIT)
        self.assertEqual(len(self.rows("encoder_unit", encoder_unit_id=self.UNIT["encoder_unit_id"])), 1)
        self.assertEqual(self.rows("host_unit", encoder_unit_id=self.UNIT["encoder_unit_id"])[0]["device"], "pci-0000:04:00.0")

    def test_add_unit_on_unknown_host_is_refused(self):
        self.refused("add-unit", dict(self.UNIT, host="nope"), "host host='nope' does not exist", "add it first with add-host")

    def test_add_unit_by_render_node_is_refused(self):
        self.refused("add-unit", dict(self.UNIT, device="/dev/dri/renderD128"), "a device addressed by render node",
                     refusals.CONSTRAINT_FIXES["host_unit_device_by_slot"][1])

    def test_add_unit_differing_from_existing_is_refused(self):
        body = dict(self.UNIT, encoder_unit_id=B580, card="Arc B570")
        self.refused("add-unit", body, f"encoder_unit '{B580}' exists as ('intel', 'Arc B580', 'iHD 26.2.2', 'qsv', 'av1')")

    def test_add_unit_twice_is_refused(self):
        body = dict(self.UNIT, encoder_unit_id=B580, card="Arc B580", device="/dev/dri/by-path/pci-0000:03:00.0-render")
        self.refused("add-unit", body, "host_unit already has a row with that host, encoder_unit_id", refusals.UNIQUE_FIXES["host_unit"])

    def test_add_unit_with_a_known_identity_under_a_new_id_is_refused(self):
        body = dict(self.UNIT, card="Arc B580")   # the B580's identity under another id
        self.refused("add-unit", body, "encoder_unit already has a row with that vendor, card, driver, frontend, codec")

    def test_add_unit_on_a_host_a_shipped_lane_is_not_routed_to_is_refused(self):
        # routing is complete per shipped lane: the AV1 lane ships, so a second AV1 host needs a ship or an exclusion first
        body = dict(self.UNIT, encoder_unit_id=B580, card="Arc B580", host="htpc-01", device="pci-0000:01:00.0")
        self.refused("add-unit", body, "x_supported_lane_not_routed: ", self.store.checks["x_supported_lane_not_routed"]["fix"])

    # ---- add-concept
    def test_add_concept_writes_the_row(self):
        self.ok("add-concept", {"canonical_id": "lookahead", "description": "frames of lookahead"})
        self.assertEqual(len(self.rows("canonical_concept", canonical_id="lookahead")), 1)

    def test_add_concept_twice_is_refused(self):
        self.refused("add-concept", {"canonical_id": "preset", "description": "again"},
                     "canonical_concept already has a row with that canonical_id", refusals.UNIQUE_DEFAULT)

    # ---- add-setting
    SETTING = {"setting_id": "nvenc.multipass", "flag": "-multipass", "frontend": "nvenc", "kind": "option", "subsystem": "rate_control",
               "value_type": "enum", "is_generic": False, "enum_values": ["disabled", "qres", "fullres"], "roles": [],
               "scope": [{"encoder_unit_id": A4000, "applies": True, "default_value": "disabled", "default_is_measured": False}]}

    def test_add_setting_writes_setting_enum_roles_and_scope(self):
        self.ok("add-setting", dict(self.SETTING, roles=["preset"]))
        (row,) = self.rows("setting", setting_id="nvenc.multipass")
        self.assertEqual(row["is_generic"], 0)
        self.assertEqual([r["value"] for r in self.rows("setting_enum_value", setting_id="nvenc.multipass")], ["disabled", "fullres", "qres"])
        self.assertEqual([r["canonical_id"] for r in self.rows("setting_role", setting_id="nvenc.multipass")], ["preset"])
        (scope,) = self.rows("setting_scope", setting_id="nvenc.multipass")
        self.assertEqual((scope["applies"], scope["default_value"], scope["default_is_measured"]), (1, "disabled", 0))

    def test_add_setting_generic_with_frontend_is_refused(self):
        self.refused("add-setting", dict(self.SETTING, is_generic=True), "a generic setting that names a frontend",
                     refusals.CONSTRAINT_FIXES["setting_generic_has_no_frontend"][1])

    def test_add_setting_unknown_role_is_refused(self):
        self.refused("add-setting", dict(self.SETTING, roles=["nope"]), "canonical_concept canonical_id='nope' does not exist", "add it first with add-concept")

    def test_add_setting_scope_on_unknown_unit_is_refused(self):
        body = dict(self.SETTING, scope=[dict(self.SETTING["scope"][0], encoder_unit_id="nope")])
        self.refused("add-setting", body, "encoder_unit encoder_unit_id='nope' does not exist", "add it first with add-unit")

    def test_add_setting_unknown_kind_is_refused(self):
        self.refused("add-setting", dict(self.SETTING, kind="knob"), "kind must be one of quality_anchor, mode_selector, ordinal, option")

    # ---- add-lane
    LANE = {"lane": "m4-ipad-le1080p-sdr-b", "codec": "av1", "decision_rule": "incumbent", "input_width_max": 1920,
            "input_dynamic_range": "sdr", "output_resolution": "native", "output_dynamic_range": "sdr", "hdr_handling": "n/a",
            "audio": "all default-flagged, else stream 0", "subtitles": "text -> mov_text", "score_height": 1548,
            "bitrate_cap_binds": "never", "has_content": True, "steps": ["probe", "remux", "quality-target-encode"]}

    def test_add_lane_writes_lane_and_steps(self):
        self.ok("add-lane", self.LANE)
        (row,) = self.rows("lane", lane=self.LANE["lane"])
        self.assertEqual((row["has_content"], row["input_width_min"], row["min_content_rate"]), (1, None, None))
        self.assertEqual([r["step"] for r in self.rows("lane_step", lane=self.LANE["lane"])], ["probe", "quality-target-encode", "remux"])

    def test_add_lane_target_without_score_target_is_refused(self):
        self.refused("add-lane", dict(self.LANE, decision_rule="target"), "a target-bound lane without a score target",
                     refusals.CONSTRAINT_FIXES["lane_target_has_score_target"][1])

    def test_add_lane_cap_without_constant_is_refused(self):
        self.refused("add-lane", dict(self.LANE, decision_rule="cap"), "a cap-bound lane with no cap constant")

    def test_add_lane_binds_without_constant_is_refused(self):
        self.refused("add-lane", dict(self.LANE, bitrate_cap_binds="rarely"), "a cap that binds with no constant")

    def test_add_lane_unknown_cap_constant_is_refused(self):
        self.refused("add-lane", dict(self.LANE, decision_rule="cap", bitrate_cap_binds="rarely", bitrate_cap_constant="NOPE"),
                     "constant name='NOPE' does not exist", "add it first with add-constant")

    def test_add_lane_with_content_and_no_population_is_refused(self):
        # the library is scanned (the fixture has titles) and nothing fits 5000 px wide: an empty population
        self.refused("add-lane", dict(self.LANE, input_width_min=5000, input_width_max=None), "x_has_content_but_empty: ",
                     self.store.checks["x_has_content_but_empty"]["fix"])

    def test_add_lane_with_content_before_any_inventory_is_allowed(self):
        empty = Store()
        self.addCleanup(empty.close)
        status, _ = post(client_for(empty), "/catalogue/add-lane", self.LANE)
        self.assertEqual(status, 200)

    def test_add_lane_unknown_step_is_refused(self):
        self.refused("add-lane", dict(self.LANE, steps=["transcode"]), "step must be one of probe, remux, quality-target-encode, bitrate-target-encode")

    # ---- add-constant
    def test_add_constant_writes_the_row(self):
        self.ok("add-constant", {"name": "FLOOR", "unit": "kbps", "provenance": "policy", "value": 800, "reason": "below it the probe is noise"})
        self.assertEqual(self.rows("constant", name="FLOOR")[0]["value"], 800.0)

    def test_add_constant_measured_with_value_is_refused(self):
        self.refused("add-constant", {"name": "X", "unit": "u", "provenance": "measured", "value": 1.0},
                     "a measured constant with a typed value", refusals.CONSTRAINT_FIXES["constant_value_iff_not_measured"][1])

    def test_add_constant_policy_without_reason_is_refused(self):
        self.refused("add-constant", {"name": "X", "unit": "u", "provenance": "policy", "value": 1.0}, "a policy constant with no reason")

    def test_add_constant_derived_without_inputs_is_refused(self):
        self.refused("add-constant", {"name": "X", "unit": "u", "provenance": "derived", "value": 1.0, "precision": "±10%"},
                     "a derived constant without its inputs and precision")

    def test_add_measured_constant_before_calibration_is_allowed(self):
        self.ok("add-constant", {"name": "X", "unit": "u", "provenance": "measured", "cites_json": '["RESULTS §1"]'})

    # ---- scope-constant
    def test_scope_constant_writes_the_scope(self):
        self.ok("scope-constant", {"name": "MARGIN", "lanes": ["kids-ipad-standard-sdr", "kids-ipad-standard-hdr"]})
        self.assertEqual(len(self.rows("constant_scope", name="MARGIN")), 6)

    def test_scope_constant_on_unknown_lane_is_refused(self):
        self.refused("scope-constant", {"name": "MARGIN", "lanes": ["nope"]}, "lane lane='nope' does not exist", "add it first with add-lane")

    def test_scope_constant_of_an_uncalibrated_constant_to_a_shipped_lane_is_refused(self):
        self.ok("add-constant", {"name": "X", "unit": "u", "provenance": "measured"})
        self.refused("scope-constant", {"name": "X", "lanes": ["m4-ipad-le1080p-sdr"]}, "x_measured_constant_never_calibrated: ")

    # ---- add-ladder
    def test_add_ladder_writes_the_rungs(self):
        empty = Store()
        self.addCleanup(empty.close)
        client = client_for(empty)
        self.assertEqual(post(client, "/catalogue/add-ladder", {"ladder_id": "av1", "codec": "av1", "rungs": [20, 15, 24]})[0], 200)
        self.assertEqual([r["rung"] for r in empty.rows("ladder_rung")], [15, 20, 24])

    def test_second_ladder_for_a_codec_is_refused(self):
        self.refused("add-ladder", {"ladder_id": "av1-b", "codec": "av1", "rungs": [15]}, "ladder already has a row with that codec",
                     refusals.UNIQUE_FIXES["ladder"])

    # ---- author-chain
    CHAIN = {"lane": "m4-ipad-gt1080p-hdr", "host": "media-01", "encoder_unit_id": A4000, "vf_template": "hwupload_cuda,scale_cuda=..."}

    def test_author_chain_writes_the_row(self):
        self.ok("author-chain", self.CHAIN)
        self.assertEqual(len(self.rows("chain", lane="m4-ipad-gt1080p-hdr")), 1)

    def test_author_chain_for_a_unit_not_on_the_host_is_refused(self):
        self.refused("author-chain", dict(self.CHAIN, host="eta"), f"host_unit host='eta', encoder_unit_id='{A4000}' does not exist",
                     "add it first with add-unit")

    def test_author_chain_twice_is_refused(self):
        body = {"lane": "m4-ipad-le1080p-sdr", "host": "media-01", "encoder_unit_id": B580, "vf_template": "null"}
        self.refused("author-chain", body, "chain already has a row with that lane, host, encoder_unit_id", refusals.UNIQUE_FIXES["chain"])

    # ---- add-scorer
    SCORER = {"host": "eta-wsl", "ffvship": ["/usr/local/bin/FFVship"], "score_ffmpeg": ["/usr/lib/jellyfin-ffmpeg/ffmpeg", "-hide_banner"],
              "metric_backend": "libvmaf_cuda", "gpu_id": 0, "cache_dir": "/home/sweep/cache"}

    def test_add_scorer_writes_compact_argv(self):
        self.ok("add-scorer", self.SCORER)
        (row,) = self.rows("scorer", host="eta-wsl")
        self.assertEqual(row["ffvship"], '["/usr/local/bin/FFVship"]')
        self.assertEqual(json.loads(row["score_ffmpeg"]), self.SCORER["score_ffmpeg"])

    def test_add_scorer_unknown_backend_is_refused(self):
        self.refused("add-scorer", dict(self.SCORER, metric_backend="vmaf"), "metric_backend must be one of libvmaf, libvmaf_cuda")

    def test_add_scorer_on_unknown_host_is_refused(self):
        self.refused("add-scorer", dict(self.SCORER, host="nope"), "host host='nope' does not exist")

    def test_add_scorer_twice_is_refused(self):
        self.refused("add-scorer", dict(self.SCORER, host="media-01-score"), "scorer already has a row with that host")

    # ---- set-floor
    def test_set_floor_writes_a_floor_the_measured_rate_meets(self):
        self.ok("set-floor", {"lane": "m4-ipad-le1080p-sdr", "min_content_rate": 20.0})
        self.assertEqual(self.rows("lane", lane="m4-ipad-le1080p-sdr")[0]["min_content_rate"], 20.0)

    def test_set_floor_above_the_measured_rate_is_refused(self):
        text = self.refused("set-floor", {"lane": "m4-ipad-le1080p-sdr", "min_content_rate": 100.0}, "content_rate_meets_floor: ")
        self.assertIn("under the floor", text)

    def test_set_floor_on_an_unmeasured_lane_is_refused(self):
        text = self.refused("set-floor", {"lane": "m4-ipad-gt1080p-sdr", "min_content_rate": 1.0}, "content_rate_meets_floor: ")
        self.assertIn("UNMEASURED", text)

    def test_set_floor_to_null_reports_only(self):
        self.ok("set-floor", {"lane": "m4-ipad-gt1080p-sdr", "min_content_rate": None})

    # ---- block-host, unblock-host
    def test_block_host_writes_the_fix(self):
        self.ok("block-host", {"host": "eta", "fix": "reseat the card; the driver lost it"})
        self.assertEqual(self.rows("host", host="eta")[0]["blocked"], "reseat the card; the driver lost it")

    def test_block_host_without_a_fix_is_refused(self):
        self.refused("block-host", {"host": "eta", "fix": "  "}, "block-host without the fix")

    def test_block_host_with_an_active_run_is_refused(self):
        self.store.conn.executescript("UPDATE run SET state = 'running' WHERE run_id = 'b580-qsv-av1-screen'; "
                                      "INSERT INTO run_event VALUES ('b580-qsv-av1-screen', '2026-09-09', 'running', 'resumed', 'agent')")
        self.refused("block-host", {"host": "media-01", "fix": "the fix"}, "x_run_on_a_blocked_host: ",
                     self.store.checks["x_run_on_a_blocked_host"]["fix"])

    def test_unblock_host_clears_the_fix(self):
        self.ok("unblock-host", {"host": "htpc-01"})
        self.assertIsNone(self.rows("host", host="htpc-01")[0]["blocked"])

    def test_unblock_unknown_host_is_refused(self):
        self.refused("unblock-host", {"host": "nope"}, "host host='nope' does not exist")


if __name__ == "__main__":
    unittest.main()
