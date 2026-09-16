#!/usr/bin/env python3
"""sweep/hub/fleet.py: the rule that freezes a catalogue row once a measurement points at it, and the document's
diff against the store.

    python3 -m unittest tests.test_hub_fleet
"""
import unittest

from sweep.hub import fleet
from tests.hub_helpers import fixture_store

B580, A4000, TI5060 = "intel-b580-ihd26.2.2-qsv-av1", "nvidia-a4000-595-nvenc-hevc", "nvidia-5060ti-595-nvenc-av1"


class Freezing(unittest.TestCase):
    def setUp(self):
        self.store = fixture_store()
        self.addCleanup(self.store.close)
        self.conn = self.store.conn

    def test_a_host_is_frozen_by_a_run_and_not_by_an_identity(self):
        # eta reported an identity at its first heartbeat and has run nothing. That is the window this whole change
        # exists to keep open: an agent starting must not freeze the row that says where its files live.
        self.assertEqual([r[0] for r in self.conn.execute("SELECT host FROM host_identity WHERE host = 'eta'")], ["eta"])
        self.assertEqual(fleet.referrers(self.conn, "host", {"host": "eta"}), {})
        self.assertEqual(fleet.referrers(self.conn, "host", {"host": "htpc-01"}), {})
        frozen = fleet.referrers(self.conn, "host", {"host": "media-01"})
        self.assertEqual(set(frozen), {"run", "published", "reference_set"})
        self.assertGreater(frozen["run"], 0)

    def test_referring_row_tables_are_the_pinned_set(self):
        # a ROW table added later with a foreign key to a host or a unit lands here, so the decision to freeze on it
        # is taken rather than inherited silently in either direction
        self.assertEqual(fleet.row_referrer_tables(self.conn, "host"), ("published", "reference_set", "run"))
        self.assertEqual(fleet.row_referrer_tables(self.conn, "encoder_unit"), ("admissibility_verdict", "run", "setting_verdict"))
        self.assertEqual(fleet.FREEZE_EXEMPT, {"host_identity"})
        self.assertIn("host_identity", fleet.row_referrer_tables(self.conn, "host", exempt=False))

    def test_a_unit_is_frozen_by_a_run_or_a_verdict(self):
        self.assertEqual(fleet.referrers(self.conn, "encoder_unit", {"encoder_unit_id": TI5060}), {})
        frozen = fleet.referrers(self.conn, "encoder_unit", {"encoder_unit_id": B580})
        self.assertEqual(set(frozen), {"run", "setting_verdict", "admissibility_verdict"})
        self.assertEqual(frozen["run"], 8)

    def test_a_placement_is_frozen_by_a_run_naming_its_host_and_unit(self):
        # the 5060 Ti sits in eta and has run nothing; the A4000 sits in media-01, which has
        self.assertEqual(fleet.referrers(self.conn, "host_unit", {"host": "eta", "encoder_unit_id": TI5060}), {})
        self.assertEqual(fleet.referrers(self.conn, "host_unit", {"host": "media-01", "encoder_unit_id": A4000}), {"run": 1})

    def test_a_scorer_is_frozen_by_a_score_run_on_its_host(self):
        # what freezes a scorer is a run that recorded what it built -- no table has a foreign key to scorer at all
        self.assertEqual(fleet.referrers(self.conn, "scorer", {"host": "media-01-score"}), {"run": 2})
        self.conn.execute("INSERT INTO scorer VALUES ('eta-wsl', '[\"FFVship\"]', '[\"ffmpeg\"]', 'libvmaf_cuda', 0, '/w/cache')")
        self.assertEqual(fleet.referrers(self.conn, "scorer", {"host": "eta-wsl"}), {})


if __name__ == "__main__":
    unittest.main()
