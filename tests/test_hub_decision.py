#!/usr/bin/env python3
"""sweep/hub/api/decision.py: record-viewing, ship and exclude-route, each one checked transaction. The ship tests
use m4-ipad-gt1080p-hdr on media-01, a (lane, host) the fixture never shipped, so each refusal is set up cleanly.

    python3 -m unittest tests.test_hub_decision
"""
import json
import unittest

from sweep.hub import refusals
from tests.hub_helpers import client_for, fixture_store, post

A4000, TI5060 = "nvidia-a4000-595-nvenc-hevc", "nvidia-5060ti-595-nvenc-av1"
LANE = "m4-ipad-gt1080p-hdr"


def setting(setting_id, value, role="identity", from_constant=None):
    return {"setting_id": setting_id, "value": value, "role": role, "from_constant": from_constant}


NVENC = [setting("nvenc.preset", "p2"), setting("nvenc.tune", "uhq"), setting("nvenc.rc", "constqp")]
QUALITY = {"step": "quality-target-encode", "encoder_unit_id": A4000, "provenance": "derived", "decided_by": "measurement",
           "reason": "budget interpolation between scored qp14 and qp17", "workers": 2, "cites_json": '["RESULTS §18.4"]',
           "settings": NVENC + [setting("nvenc.qp", "15")]}
BITRATE = {"step": "bitrate-target-encode", "encoder_unit_id": A4000, "provenance": "derived", "decided_by": "measurement",
           "reason": "-b:v is CEILING x HEADROOM", "workers": 2,
           "settings": [setting("nvenc.preset", "p2"), setting("nvenc.tune", "uhq"), setting("nvenc.rc", "vbr"),
                        setting("nvenc.b_v", "17600000", "computed", "CEILING")]}
PROBE = {"step": "probe", "encoder_unit_id": A4000, "provenance": "derived", "decided_by": "policy",
         "reason": "the HEVC probe is pinned to qp 14 so it reads the same on every host", "workers": 1, "settings": NVENC + [setting("nvenc.qp", "14")]}
REMUX = {"step": "remux", "provenance": "fixed", "decided_by": "policy", "reason": "container rebuild only; no encoder decision"}
SHIP = {"lane": LANE, "host": "media-01", "rows": [QUALITY, BITRATE, PROBE, REMUX]}
VIEWING = {"kind": "pair", "window_id": "tng", "cell_a": "g-a", "cell_b": "g-b", "viewed_on": "iPad M4 13in", "viewer": "andy",
           "verdict": "same", "viewed_at": "2026-09-05"}


class Decision(unittest.TestCase):
    def setUp(self):
        self.store = fixture_store()
        self.addCleanup(self.store.close)
        self.client = client_for(self.store)

    def rows(self, table, **key):
        return [r for r in self.store.rows(table) if all(r[k] == v for k, v in key.items())]

    def call(self, path, body):
        return post(self.client, path, body)

    def starts(self, path, body, prefix):
        status, text = self.call(path, body)
        self.assertEqual(status, 422, text)
        self.assertTrue(text.startswith("REFUSING: " + prefix), text)
        return text

    def names(self, path, body, check):
        status, text = self.call(path, body)
        self.assertEqual(status, 422, text)
        self.assertIn(check, text)
        return text

    def chain(self):
        self.assertEqual(self.call("/catalogue/author-chain", {"lane": LANE, "host": "media-01", "encoder_unit_id": A4000,
                                                              "vf_template": "hwupload_cuda,scale_cuda=..."})[0], 200)

    # ---- record-viewing
    def test_record_viewing_writes_a_pair_and_returns_its_id(self):
        self.assertEqual(self.call("/decision/record-viewing", VIEWING), (200, '{"viewing_id":3}'))
        self.assertEqual(self.rows("viewing_verdict", viewing_id=3)[0]["device"], "iPad M4 13in")

    def test_record_viewing_writes_an_acceptance(self):
        body = dict(VIEWING, kind="acceptance", lane="m4-ipad-le1080p-sdr", cell_a="g-i", cell_b=None, verdict="acceptable")
        self.assertEqual(self.call("/decision/record-viewing", body), (200, '{"viewing_id":3}'))

    def test_record_viewing_on_a_discarded_encode_is_refused(self):
        self.starts("/decision/record-viewing", dict(VIEWING, cell_a="c-a24-tng"), "x_viewing_on_a_discarded_encode: ")
        self.starts("/decision/record-viewing", dict(VIEWING, cell_b="c-a24-tng"), "x_viewing_on_a_discarded_encode: ")

    def test_record_viewing_pair_with_one_encode_is_refused(self):
        self.starts("/decision/record-viewing", dict(VIEWING, cell_b=None), refusals.CONSTRAINT_FIXES["viewing_pair_has_two_cells"][0])

    def test_record_viewing_acceptance_without_a_lane_is_refused(self):
        body = dict(VIEWING, kind="acceptance", cell_b=None, verdict="acceptable")
        self.starts("/decision/record-viewing", body, refusals.CONSTRAINT_FIXES["viewing_acceptance_names_a_lane"][0])

    def test_record_viewing_verdict_that_does_not_fit_the_kind_is_refused(self):
        self.starts("/decision/record-viewing", dict(VIEWING, verdict="acceptable"), refusals.CONSTRAINT_FIXES["viewing_verdict_fits_kind"][0])

    def test_record_viewing_of_an_unknown_cell_is_refused(self):
        self.starts("/decision/record-viewing", dict(VIEWING, cell_a="nope"), "cell cell_key='nope' does not exist")

    # ---- ship
    def test_ship_writes_every_step_and_returns_the_ids(self):
        self.chain()
        self.assertEqual(self.call("/decision/ship", SHIP), (200, '{"shipped_ids":[10,11,12,13]}'))
        self.assertEqual([r["step"] for r in self.rows("shipped", lane=LANE)], ["quality-target-encode", "bitrate-target-encode", "probe", "remux"])
        self.assertEqual(self.rows("shipped_setting", shipped_id=11, setting_id="nvenc.b_v")[0]["from_constant"], "CEILING")
        self.assertEqual(self.store.check(), {})

    def test_ship_an_anchor_off_the_ladder_is_refused(self):
        self.chain()
        row = dict(QUALITY, settings=NVENC + [setting("nvenc.qp", "13")])
        self.starts("/decision/ship", dict(SHIP, rows=[row, BITRATE, PROBE, REMUX]), "x_shipped_not_a_rung: ")

    def measured_ship(self):
        return dict(SHIP, rows=[dict(QUALITY, provenance="measured", content_class_id="native-1080p-sdr"), BITRATE, PROBE, REMUX])

    def test_ship_measured_without_representation_is_refused(self):
        self.chain()
        self.names("/decision/ship", self.measured_ship(), "x_measured_without_representation")

    def test_ship_measured_on_a_class_not_for_the_lane_is_refused(self):
        self.chain()
        self.names("/decision/ship", self.measured_ship(), "x_measured_on_a_class_not_for_the_lane")

    def test_ship_measured_configuration_nobody_encoded_is_refused(self):
        self.chain()
        self.names("/decision/ship", self.measured_ship(), "measured_config_was_measured")

    def test_ship_a_constant_outside_its_scope_is_refused(self):
        self.chain()
        self.store.conn.execute("INSERT INTO constant VALUES ('OTHER', 1.0, 'u', 'policy', NULL, NULL, 'a reason', NULL)")
        row = dict(BITRATE, settings=BITRATE["settings"][:3] + [setting("nvenc.b_v", "1", "computed", "OTHER")])
        self.starts("/decision/ship", dict(SHIP, rows=[QUALITY, row, PROBE, REMUX]), "x_constant_outside_scope: ")

    def test_ship_an_encode_step_without_a_chain_is_refused(self):
        self.starts("/decision/ship", SHIP, "x_shipped_without_chain: ")

    def test_ship_a_unit_not_in_that_host_is_refused(self):
        self.chain()
        self.names("/decision/ship", dict(SHIP, rows=[dict(QUALITY, encoder_unit_id=TI5060), BITRATE, PROBE, REMUX]), "x_shipped_unit_not_on_host")

    def test_ship_leaving_a_step_unrouted_is_refused(self):
        self.chain()
        self.starts("/decision/ship", dict(SHIP, rows=[QUALITY, BITRATE, PROBE]), "x_supported_lane_not_routed: ")

    def test_ship_a_route_that_is_excluded_is_refused(self):
        self.chain()
        self.assertEqual(self.call("/decision/exclude-route", {"lane": LANE, "host": "media-01", "reason": "not this box"})[0], 200)
        self.starts("/decision/ship", SHIP, "x_routed_and_excluded: ")

    def test_ship_under_a_floor_never_timed_is_refused(self):
        self.chain()
        self.assertEqual(self.call("/catalogue/set-floor", {"lane": LANE, "min_content_rate": 1.0})[0], 200)
        self.assertIn("UNMEASURED", self.starts("/decision/ship", SHIP, "content_rate_meets_floor: "))

    def test_ship_with_a_measured_constant_never_calibrated_in_scope_is_refused(self):
        self.chain()
        self.assertEqual(self.call("/catalogue/add-constant", {"name": "NEW", "unit": "u", "provenance": "measured"})[0], 200)
        self.assertEqual(self.call("/catalogue/scope-constant", {"name": "NEW", "lanes": [LANE]})[0], 200)
        self.starts("/decision/ship", SHIP, "x_measured_constant_never_calibrated: ")

    def test_ship_measured_without_a_class_is_refused(self):
        self.chain()
        self.starts("/decision/ship", dict(SHIP, rows=[dict(QUALITY, provenance="measured"), BITRATE, PROBE, REMUX]),
                    refusals.CONSTRAINT_FIXES["shipped_measured_has_class"][0])

    def test_ship_a_policy_decision_without_a_reason_is_refused(self):
        self.chain()
        self.starts("/decision/ship", dict(SHIP, rows=[QUALITY, BITRATE, dict(PROBE, reason=None), REMUX]),
                    refusals.CONSTRAINT_FIXES["shipped_policy_has_reason"][0])

    def test_ship_a_remux_that_is_not_fixed_is_refused(self):
        self.chain()
        self.starts("/decision/ship", dict(SHIP, rows=[QUALITY, BITRATE, PROBE, dict(REMUX, provenance="derived")]),
                    refusals.CONSTRAINT_FIXES["shipped_remux_is_fixed"][0])

    def test_ship_a_constant_on_an_identity_setting_is_refused(self):
        self.chain()
        row = dict(BITRATE, settings=BITRATE["settings"][:3] + [setting("nvenc.b_v", "17600000", "identity", "CEILING")])
        self.starts("/decision/ship", dict(SHIP, rows=[QUALITY, row, PROBE, REMUX]), refusals.CONSTRAINT_FIXES["shipped_setting_constant_is_computed"][0])

    def test_ship_a_step_the_lane_lacks_is_refused(self):
        self.chain()
        self.starts("/decision/ship", dict(SHIP, rows=[dict(QUALITY, step="transcode")]),
                    f"lane_step lane='{LANE}', step='transcode' does not exist -- add it first with add-lane")

    def test_ship_twice_is_refused(self):
        body = {"lane": "m4-ipad-le1080p-sdr", "host": "media-01", "rows": [dict(REMUX)]}
        self.starts("/decision/ship", body, "shipped already has a row with that lane, host, step")

    # ---- exclude-route
    def test_exclude_route_writes_the_reason(self):
        self.assertEqual(self.call("/decision/exclude-route", {"lane": LANE, "host": "eta", "reason": "no HEVC unit there"}), (200, '{"ok":true}'))
        self.assertEqual(self.rows("routing_exclusion", lane=LANE)[0]["reason"], "no HEVC unit there")

    def test_exclude_route_of_a_shipped_route_is_refused(self):
        self.starts("/decision/exclude-route", {"lane": "m4-ipad-le1080p-sdr", "host": "media-01", "reason": "x"}, "x_routed_and_excluded: ")

    def test_exclude_route_on_unknown_lane_or_host_is_refused(self):
        self.starts("/decision/exclude-route", {"lane": "nope", "host": "eta", "reason": "x"}, "lane lane='nope' does not exist")
        self.starts("/decision/exclude-route", {"lane": LANE, "host": "nope", "reason": "x"}, "host host='nope' does not exist")

    def test_exclude_route_twice_is_refused(self):
        self.starts("/decision/exclude-route", {"lane": "m4-ipad-le1080p-sdr", "host": "eta", "reason": "x"},
                    "routing_exclusion already has a row with that lane, host")


if __name__ == "__main__":
    unittest.main()
