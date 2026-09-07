#!/usr/bin/env python3
"""sweep/hub/api/sample.py: pin-window, classify-cut, define-class, each one checked transaction.

    python3 -m unittest tests.test_hub_sample
"""
import unittest

from sweep.hub import refusals
from tests.hub_helpers import client_for, fixture_store, post


class Sample(unittest.TestCase):
    def setUp(self):
        self.store = fixture_store()
        self.addCleanup(self.store.close)
        self.client = client_for(self.store)

    def rows(self, table, **key):
        return [r for r in self.store.rows(table) if all(r[k] == v for k, v in key.items())]

    def ok(self, verb, body):
        status, text = post(self.client, f"/sample/{verb}", body)
        self.assertEqual((status, text), (200, '{"ok":true}'))

    def refused(self, verb, body, starts, fix=None):
        status, text = post(self.client, f"/sample/{verb}", body)
        self.assertEqual(status, 422, text)
        self.assertTrue(text.startswith("REFUSING: " + starts), text)
        if fix is not None:
            self.assertTrue(text.endswith(" -- " + fix), text)
        return text

    def names(self, verb, body, check):
        """The refusal names the check, first or among the others that fire; the client sees it either way."""
        status, text = post(self.client, f"/sample/{verb}", body)
        self.assertEqual(status, 422, text)
        self.assertIn(check, text)
        return text

    # ---- pin-window
    WINDOW = {"window_id": "mrrobot-2", "title_id": "mrrobot", "ss": 2400.0, "t": 60.0, "character": "dark and noisy",
              "selected_by": "satavg+cuts v1", "selection_score": 81.0}

    def test_pin_window_writes_a_pinned_window(self):
        self.ok("pin-window", self.WINDOW)
        (row,) = self.rows("window", window_id="mrrobot-2")
        self.assertEqual((row["origin"], row["notes"]), ("pinned", None))

    def test_pin_window_on_unknown_title_is_refused(self):
        self.refused("pin-window", dict(self.WINDOW, title_id="nope"), "title title_id='nope' does not exist", "add it first with an inventory run")

    def test_pin_window_twice_is_refused(self):
        self.refused("pin-window", dict(self.WINDOW, window_id="tng"), "window already has a row with that window_id", refusals.UNIQUE_FIXES["window"])

    # ---- classify-cut
    CUT = {"cut_id": "tng.ref", "check_name": "dv_rpu", "reason": "no RPU: the source is HDR10, the check expects DV", "checked_at": "2026-08-27"}

    def test_classify_cut_writes_a_classified_check(self):
        self.ok("classify-cut", self.CUT)
        (row,) = self.rows("cut_check", cut_id="tng.ref", check_name="dv_rpu")
        self.assertEqual(row["result"], "classified")

    def test_classify_cut_on_unknown_cut_is_refused(self):
        self.refused("classify-cut", dict(self.CUT, cut_id="nope"), "cut cut_id='nope' does not exist", "add it first with a materialise run")

    def test_classify_cut_without_a_reason_is_refused(self):
        self.refused("classify-cut", dict(self.CUT, reason=""), "cut 'tng.ref' classified on dv_rpu with no reason")

    # ---- define-class
    CLASS = {"content_class_id": "native-1080p-sdr-b", "name": "native-1080p-sdr-b", "reference_set_id": "stage-1080p",
             "lanes": ["m4-ipad-le1080p-sdr"], "members": ["tng", "parks"],
             "strata": [{"stratum": "remux", "kind": "inventory", "definition": "t.source_type = 'remux'"},
                        {"stratum": "well-lit", "kind": "character", "definition": "well-lit grain-free", "share_estimate": 0.2}]}

    def test_define_class_writes_class_lanes_members_and_strata(self):
        self.ok("define-class", self.CLASS)
        self.assertEqual([r["lane"] for r in self.rows("content_class_lane", content_class_id="native-1080p-sdr-b")], ["m4-ipad-le1080p-sdr"])
        self.assertEqual([r["window_id"] for r in self.rows("content_class_member", content_class_id="native-1080p-sdr-b")], ["parks", "tng"])
        strata = {r["stratum"]: (r["min_windows"], r["share_estimate"]) for r in self.rows("content_class_stratum", content_class_id="native-1080p-sdr-b")}
        self.assertEqual(strata, {"remux": (1, None), "well-lit": (1, 0.2)})
        self.assertEqual(self.store.check(), {})

    def test_define_class_with_an_uncovered_stratum_is_refused(self):
        body = dict(self.CLASS, strata=[{"stratum": "web", "kind": "inventory", "definition": "t.source_type = 'web'"}], members=["tng"])
        self.refused("define-class", body, "strata_covered: ")

    def test_define_class_serving_no_lane_is_refused(self):
        # the cuts' chain lane is unserved too, and that view comes first; both are named
        self.names("define-class", dict(self.CLASS, lanes=[]), "x_class_serves_no_lane")

    def test_define_class_with_a_member_without_a_reference_cut_is_refused(self):
        self.store.conn.execute("INSERT INTO window VALUES ('extra','tng',10.0,60.0,NULL,'pinned',NULL,NULL,NULL)")
        self.refused("define-class", dict(self.CLASS, members=["tng", "extra"], strata=[]), "x_member_without_reference_cut: ")

    def test_define_class_with_an_unchecked_reference_cut_is_refused(self):
        self.store.conn.executescript(
            "INSERT INTO window VALUES ('extra','tng',10.0,60.0,NULL,'pinned',NULL,NULL,NULL); "
            "INSERT INTO cut VALUES ('extra.ref','stage-1080p','extra','reference','m4-ipad-le1080p-sdr','media-01','intel-b580-ihd26.2.2-qsv-av1','sha-extra',1,1439,NULL)")
        self.refused("define-class", dict(self.CLASS, members=["tng", "extra"], strata=[]), "x_reference_cut_unchecked: ")

    def test_define_class_whose_cuts_were_built_through_an_unserved_lanes_chain_is_refused(self):
        self.refused("define-class", dict(self.CLASS, lanes=["m4-ipad-gt1080p-sdr"]), "x_cut_chain_not_a_served_lane: ")

    def test_define_class_character_stratum_without_a_share_is_refused(self):
        body = dict(self.CLASS, strata=[{"stratum": "dark", "kind": "character", "definition": "dark and noisy"}])
        self.refused("define-class", body, "a character stratum with no share estimate", refusals.CONSTRAINT_FIXES["stratum_character_has_share"][1])

    def test_define_class_on_unknown_reference_set_is_refused(self):
        self.refused("define-class", dict(self.CLASS, reference_set_id="nope"), "reference_set reference_set_id='nope' does not exist")

    def test_define_class_for_unknown_lane_is_refused(self):
        self.refused("define-class", dict(self.CLASS, lanes=["nope"]), "lane lane='nope' does not exist")

    def test_define_class_with_unknown_window_is_refused(self):
        self.refused("define-class", dict(self.CLASS, members=["nope"]), "window window_id='nope' does not exist", "add it first with pin-window")

    def test_define_class_with_a_taken_name_is_refused(self):
        self.refused("define-class", dict(self.CLASS, name="native-1080p-sdr"), "content_class already has a row with that name",
                     refusals.UNIQUE_FIXES["content_class"])


if __name__ == "__main__":
    unittest.main()
