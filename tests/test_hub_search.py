#!/usr/bin/env python3
"""sweep/hub/api/search.py: author-search and set-shipping-arm, each one checked transaction.

    python3 -m unittest tests.test_hub_search
"""
import unittest

from sweep.hub import refusals
from tests.hub_helpers import client_for, fixture_store, post

B580 = "intel-b580-ihd26.2.2-qsv-av1"
BASE = {"arm_id": "arm2-a", "name": "cqp-preset4-bs0", "role": "base", "settings": [{"setting_id": "qsv.preset", "value": "4"}, {"setting_id": "qsv.b_strategy", "value": "0"}]}
INCUMBENT = {"arm_id": "arm2-i", "name": "incumbent", "role": "incumbent", "anchor_value": "30", "accepted_by_viewing": 2,
             "settings": [{"setting_id": "qsv.preset", "value": "1"}, {"setting_id": "qsv.b_strategy", "value": "1"}]}
SEARCH = {"search_id": "b580-qsv-av1-2", "content_class_id": "native-1080p-sdr", "encoder_unit_id": B580, "anchor_setting_id": "qsv.q",
          "score_height": 1548, "arms": [BASE], "coarse_rungs": [24, 30]}


class Search(unittest.TestCase):
    def setUp(self):
        self.store = fixture_store()
        self.addCleanup(self.store.close)
        self.client = client_for(self.store)

    def rows(self, table, **key):
        return [r for r in self.store.rows(table) if all(r[k] == v for k, v in key.items())]

    def ok(self, verb, body):
        status, text = post(self.client, f"/search/{verb}", body)
        self.assertEqual((status, text), (200, '{"ok":true}'))

    def names(self, verb, body, check):
        status, text = post(self.client, f"/search/{verb}", body)
        self.assertEqual(status, 422, text)
        self.assertIn(check, text)
        return text

    def starts(self, verb, body, prefix):
        status, text = post(self.client, f"/search/{verb}", body)
        self.assertEqual(status, 422, text)
        self.assertTrue(text.startswith("REFUSING: " + prefix), text)
        return text

    def make_target_lane(self):
        """The served lane becomes target-bound, and the fixture search's targets get their viewing, so the store stays clean."""
        self.store.conn.executescript("UPDATE lane SET decision_rule = 'target', score_target = 78.1 WHERE lane = 'm4-ipad-le1080p-sdr'; "
                                      "UPDATE search_target SET viewing_id = 2")
        self.assertEqual(self.store.check(), {})

    # ---- author-search
    def test_author_search_writes_search_arms_settings_rungs_and_targets(self):
        self.ok("author-search", dict(SEARCH, targets=[{"metric": "ssimulacra2", "statistic": "mean", "target": 80.0}]))
        (search,) = self.rows("search", search_id="b580-qsv-av1-2")
        self.assertIsNone(search["shipping_arm_id"])
        self.assertEqual([r["arm_id"] for r in self.rows("arm", search_id="b580-qsv-av1-2")], ["arm2-a"])
        self.assertEqual(len(self.rows("arm_setting", arm_id="arm2-a")), 2)
        self.assertEqual([r["rung"] for r in self.rows("search_coarse_rung", search_id="b580-qsv-av1-2")], [24, 30])
        self.assertEqual(len(self.rows("search_target", search_id="b580-qsv-av1-2")), 1)
        self.assertEqual(self.store.check(), {})

    def test_author_search_without_exactly_one_base_arm_is_refused(self):
        self.starts("author-search", dict(SEARCH, arms=[BASE, dict(BASE, arm_id="arm2-b", name="second base")]), "x_search_arm_roles: ")
        self.starts("author-search", dict(SEARCH, arms=[]), "x_search_arm_roles: ")

    def test_author_search_with_an_unhonoured_setting_is_refused(self):
        arm = dict(BASE, settings=BASE["settings"] + [{"setting_id": "qsv.tile_cols", "value": "2"}])
        self.starts("author-search", dict(SEARCH, arms=[arm]), "x_arm_setting_not_honoured: ")

    def test_author_search_at_a_height_no_served_lane_uses_is_refused(self):
        self.starts("author-search", dict(SEARCH, score_height=1080), "x_search_height_not_a_lane_height: ")

    def test_author_search_for_an_incumbent_lane_without_an_incumbent_arm_is_refused(self):
        self.store.conn.execute("UPDATE lane SET decision_rule = 'incumbent' WHERE lane = 'm4-ipad-le1080p-sdr'")
        self.assertEqual(self.store.check(), {})
        self.starts("author-search", SEARCH, "x_incumbent_rule_without_incumbent_arm: ")

    def test_author_search_for_a_target_lane_without_targets_is_refused(self):
        self.make_target_lane()
        self.starts("author-search", SEARCH, "x_target_rule_without_targets: ")

    def test_author_search_target_without_its_viewing_is_refused(self):
        self.make_target_lane()
        self.starts("author-search", dict(SEARCH, targets=[{"metric": "ssimulacra2", "statistic": "mean", "target": 80.0}]), "x_target_without_a_viewing: ")

    def test_author_search_incumbent_no_viewing_accepted_is_refused(self):
        self.starts("author-search", dict(SEARCH, arms=[BASE, dict(INCUMBENT, accepted_by_viewing=None)]), "x_incumbent_arm_not_viewed: ")

    def test_author_search_incumbent_differing_from_the_viewed_encode_is_refused(self):
        arm = dict(INCUMBENT, settings=[{"setting_id": "qsv.preset", "value": "4"}, {"setting_id": "qsv.b_strategy", "value": "1"}])
        self.starts("author-search", dict(SEARCH, arms=[BASE, arm]), "incumbent_viewing_matches_arm: ")

    def test_author_search_with_an_incumbent_the_viewing_accepted_is_allowed(self):
        self.ok("author-search", dict(SEARCH, arms=[BASE, INCUMBENT]))

    def test_author_search_on_an_anchor_never_tested_admissible_is_refused(self):
        self.starts("author-search", dict(SEARCH, anchor_setting_id="qsv.tile_cols"), "x_search_mode_not_admissible: ")

    def test_author_search_incumbent_without_a_pinned_anchor_is_refused(self):
        self.starts("author-search", dict(SEARCH, arms=[BASE, dict(INCUMBENT, anchor_value=None)]),
                    refusals.CONSTRAINT_FIXES["arm_incumbent_is_pinned"][0])

    def test_author_search_base_with_a_pinned_anchor_is_refused(self):
        self.starts("author-search", dict(SEARCH, arms=[dict(BASE, anchor_value="30")]), refusals.CONSTRAINT_FIXES["arm_incumbent_is_pinned"][0])

    def test_author_search_on_unknown_anchor_setting_is_refused(self):
        self.starts("author-search", dict(SEARCH, anchor_setting_id="nope"), "setting setting_id='nope' does not exist")

    # ---- set-shipping-arm
    def test_set_shipping_arm_writes_the_arm(self):
        self.ok("set-shipping-arm", {"search_id": "b580-qsv-av1", "arm_id": "arm-a"})
        self.assertEqual(self.rows("search", search_id="b580-qsv-av1")[0]["shipping_arm_id"], "arm-a")

    def test_set_shipping_arm_from_another_search_is_refused(self):
        self.ok("author-search", SEARCH)
        self.starts("set-shipping-arm", {"search_id": "b580-qsv-av1", "arm_id": "arm2-a"}, "x_shipping_arm_not_in_search: ")

    def test_set_shipping_arm_whose_ladder_is_incomplete_is_refused(self):
        self.starts("set-shipping-arm", {"search_id": "b580-qsv-av1", "arm_id": "arm-b"}, "shipping_arm_ladder_complete: ")

    def test_set_shipping_arm_before_the_incumbent_is_scored_is_refused(self):
        self.store.conn.executescript("UPDATE search SET shipping_arm_id = NULL; DELETE FROM score WHERE cell_key = 'c-i30-parks'; "
                                      "UPDATE encode SET kept = 1 WHERE cell_key = 'c-i30-parks'")
        self.assertEqual(self.store.check(), {})     # a precondition, scoped to the moment the arm is named
        self.starts("set-shipping-arm", {"search_id": "b580-qsv-av1", "arm_id": "arm-a"}, "incumbent_arm_scored: ")


if __name__ == "__main__":
    unittest.main()
