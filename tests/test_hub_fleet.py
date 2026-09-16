#!/usr/bin/env python3
"""sweep/hub/fleet.py: the rule that freezes a catalogue row once a measurement points at it, and the document's
diff against the store.

    python3 -m unittest tests.test_hub_fleet
"""
import copy
import json
import unittest

from sweep.hub import fleet
from sweep.hub.refusals import Refusal
from sweep.hub.store import Store
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


def document_of(store):
    """The fixture's own fleet, in the document's shape: re-applying it must be a no-op."""
    units = {u["encoder_unit_id"]: u for u in store.rows("encoder_unit")}
    return {
        "hosts": [{k: r[k] for k in fleet.COLUMNS["host"]} for r in store.rows("host")],
        "units": [dict({k: units[p["encoder_unit_id"]][k] for k in fleet.COLUMNS["encoder_unit"]},
                       host=p["host"], device=p["device"]) for p in store.rows("host_unit")],
        "scorers": [dict({k: r[k] for k in ("host", "metric_backend", "gpu_id", "cache_dir")},
                         ffvship=json.loads(r["ffvship"]), score_ffmpeg=json.loads(r["score_ffmpeg"]))
                    for r in store.rows("scorer")],
    }


class Diff(unittest.TestCase):
    def setUp(self):
        self.store = fixture_store()
        self.addCleanup(self.store.close)
        self.conn = self.store.conn
        self.doc = document_of(self.store)

    def test_the_fixtures_own_rows_re_applied_are_unchanged(self):
        d = fleet.diff(self.conn, self.doc)
        self.assertEqual((d["created"], d["updated"], d["removed"]),
                         ({"hosts": [], "units": [], "scorers": []},) * 3)
        self.assertEqual(d["unchanged"], 6 + 3 + 1)

    def test_a_changed_field_on_a_host_is_an_update_carrying_both_values(self):
        (htpc,) = [h for h in self.doc["hosts"] if h["host"] == "htpc-01"]
        was, htpc["work_root"] = htpc["work_root"], "/run/media/system/data/gpu-encoder-sweep"
        d = fleet.diff(self.conn, self.doc)
        self.assertEqual(d["updated"]["hosts"], [{"name": "htpc-01",
                                                  "fields": {"work_root": [was, "/run/media/system/data/gpu-encoder-sweep"]}}])
        self.assertEqual(d["unchanged"], 9)

    def test_a_new_host_is_a_creation(self):
        self.doc["hosts"].append({"host": "htpc-02", "ssh_host": "htpc-02", "os": "linux", "machine": "htpc-02",
                                  "work_root": "/data/sweep", "share_root": "/share",
                                  "ffmpeg": None, "local_view": None, "notes": None})
        d = fleet.diff(self.conn, self.doc)
        self.assertEqual(d["created"]["hosts"], ["htpc-02"])
        self.assertEqual(d["unchanged"], 10)

    def test_an_omitted_host_is_a_removal(self):
        self.doc["hosts"] = [h for h in self.doc["hosts"] if h["host"] != "htpc-01"]
        d = fleet.diff(self.conn, self.doc)
        self.assertEqual(d["removed"]["hosts"], ["htpc-01"])

    def test_an_absent_key_removes_nothing_and_an_empty_list_removes_all(self):
        del self.doc["scorers"]
        self.assertEqual(fleet.diff(self.conn, self.doc)["removed"]["scorers"], [])
        self.assertEqual(fleet.diff(self.conn, self.doc)["unchanged"], 9)      # the scorer is not counted either
        self.doc["scorers"] = []
        self.assertEqual(fleet.diff(self.conn, self.doc)["removed"]["scorers"], ["media-01-score"])

    def test_a_placement_and_its_device_are_diffed_per_host(self):
        (a4000,) = [u for u in self.doc["units"] if u["encoder_unit_id"] == A4000]
        a4000["device"] = "0000:09:00.0"
        d = fleet.diff(self.conn, self.doc)
        self.assertEqual(d["updated"]["units"], [{"name": f"{A4000}@media-01",
                                                  "fields": {"device": ["pci-0000:41:00.0", "0000:09:00.0"]}}])

    def test_two_entries_disagreeing_on_one_unit_identity_refuse(self):
        (a4000,) = [u for u in self.doc["units"] if u["encoder_unit_id"] == A4000]
        self.doc["units"].append(dict(a4000, host="eta", device="0000:0C:00.0", driver="616.92"))
        with self.assertRaises(Refusal) as cm:
            fleet.diff(self.conn, self.doc)
        self.assertIn(A4000, cm.exception.what)
        self.assertIn("driver", cm.exception.what)


FLEET = {
    "hosts": [
        {"host": "nas-01", "ssh_host": "nas-01", "os": "linux", "machine": "nas-01",
         "work_root": "/data", "share_root": "/share", "ffmpeg": None, "local_view": None, "notes": None},
        {"host": "box", "ssh_host": "box", "os": "linux", "machine": "box",
         "work_root": "/w", "share_root": "/share", "ffmpeg": "/ff/ffmpeg", "local_view": None, "notes": None},
    ],
    "units": [
        {"encoder_unit_id": A4000, "vendor": "nvidia", "card": "NVIDIA RTX A4000", "driver": "595",
         "frontend": "nvenc", "codec": "hevc", "host": "box", "device": "0000:01:00.0"},
    ],
    "scorers": [
        {"host": "box", "ffvship": ["/usr/local/bin/FFVship"], "score_ffmpeg": ["/ff/ffmpeg"],
         "metric_backend": "libvmaf_cuda", "gpu_id": 0, "cache_dir": "/w/cache"},
    ],
}


class ApplyFromNothing(unittest.TestCase):
    """The day one workflow: a fleet authored into an empty store, then corrected."""

    def setUp(self):
        self.store = Store()
        self.addCleanup(self.store.close)

    def apply(self, doc):
        with self.store.transaction() as conn:
            return fleet.apply(conn, doc)

    def test_a_fleet_is_created_from_nothing_and_applying_it_again_changes_nothing(self):
        plan = self.apply(copy.deepcopy(FLEET))
        self.assertEqual(plan["created"], {"hosts": ["box", "nas-01"], "units": [f"{A4000}@box"], "scorers": ["box"]})
        self.assertEqual([r["host"] for r in self.store.rows("host")], ["box", "nas-01"])
        self.assertEqual(self.store.rows("scorer")[0]["ffvship"], '["/usr/local/bin/FFVship"]')
        self.assertEqual(self.store.rows("host_unit")[0]["device"], "0000:01:00.0")
        again = self.apply(copy.deepcopy(FLEET))
        self.assertEqual(again["unchanged"], 4)
        self.assertEqual((again["created"], again["updated"], again["removed"]),
                         ({"hosts": [], "units": [], "scorers": []},) * 3)

    def test_a_corrected_field_updates_while_nothing_has_run(self):
        self.apply(copy.deepcopy(FLEET))
        doc = copy.deepcopy(FLEET)
        doc["hosts"][1]["work_root"] = "/w/gpu-encoder-sweep"
        plan = self.apply(doc)
        self.assertEqual(plan["updated"]["hosts"], [{"name": "box", "fields": {"work_root": ["/w", "/w/gpu-encoder-sweep"]}}])
        self.assertEqual([r["work_root"] for r in self.store.rows("host") if r["host"] == "box"], ["/w/gpu-encoder-sweep"])

    def test_a_removed_placement_takes_its_orphaned_unit_with_it(self):
        self.apply(copy.deepcopy(FLEET))
        doc = copy.deepcopy(FLEET)
        doc["units"] = []
        plan = self.apply(doc)
        self.assertEqual(plan["removed"]["units"], [f"{A4000}@box"])
        self.assertEqual(self.store.rows("host_unit"), [])
        self.assertEqual(self.store.rows("encoder_unit"), [])        # nothing places it any more

    def test_a_host_nobody_declares_is_refused_for_the_scorer_that_names_it(self):
        doc = copy.deepcopy(FLEET)
        doc["scorers"][0]["host"] = "ghost"
        with self.assertRaises(Refusal) as cm:
            self.apply(doc)
        self.assertEqual(str(cm.exception), "REFUSING: host host='ghost' does not exist -- add it first with add-host")


class ApplyAgainstMeasurements(unittest.TestCase):
    """The fixture has run things: what may still change, and what may not."""

    def setUp(self):
        self.store = fixture_store()
        self.addCleanup(self.store.close)
        self.doc = document_of(self.store)

    def apply(self, doc):
        with self.store.transaction() as conn:
            return fleet.apply(conn, doc)

    def host(self, name):
        (h,) = [h for h in self.doc["hosts"] if h["host"] == name]
        return h

    def test_a_frozen_field_refuses_naming_the_field_both_values_and_what_holds_it(self):
        self.host("media-01")["work_root"] = "/mnt/data/gpu-encoder-sweep"
        with self.assertRaises(Refusal) as cm:
            self.apply(self.doc)
        self.assertRegex(str(cm.exception), r"^REFUSING: .+ -- .+$")
        for part in ("host 'media-01'", "work_root", "/mnt/data/sweep", "/mnt/data/gpu-encoder-sweep", "run row"):
            self.assertIn(part, cm.exception.what)
        self.assertEqual([h["work_root"] for h in self.store.rows("host") if h["host"] == "media-01"], ["/mnt/data/sweep"])

    def test_notes_and_ssh_host_stay_free_on_a_referenced_host(self):
        self.host("media-01").update(notes="two cards, one box", ssh_host="media-01.lan")
        plan = self.apply(self.doc)
        (row,) = [h for h in self.store.rows("host") if h["host"] == "media-01"]
        self.assertEqual((row["notes"], row["ssh_host"]), ("two cards, one box", "media-01.lan"))
        self.assertEqual(sorted(plan["updated"]["hosts"][0]["fields"]), ["notes", "ssh_host"])

    def test_removing_a_referenced_host_refuses_naming_what_names_it(self):
        self.doc["hosts"] = [h for h in self.doc["hosts"] if h["host"] != "media-01"]
        with self.assertRaises(Refusal) as cm:
            self.apply(self.doc)
        self.assertIn("media-01", cm.exception.what)
        self.assertIn("run row", cm.exception.what)
        self.assertEqual(len(self.store.rows("host")), 6)

    def test_removing_a_host_whose_only_trace_is_an_identity_takes_the_identity_with_it(self):
        # eta-wsl has reported and run nothing. The identity is the agent's own report about a row that is going.
        self.doc["hosts"] = [h for h in self.doc["hosts"] if h["host"] != "eta-wsl"]
        plan = self.apply(self.doc)
        self.assertEqual(plan["removed"]["hosts"], ["eta-wsl"])
        self.assertNotIn("eta-wsl", [h["host"] for h in self.store.rows("host")])
        self.assertNotIn("eta-wsl", [i["host"] for i in self.store.rows("host_identity")])

    def test_a_unit_identity_change_refuses_whatever_points_at_it(self):
        (unit,) = [u for u in self.doc["units"] if u["encoder_unit_id"] == B580]
        unit["driver"] = "ihd26.2.4"
        with self.assertRaises(Refusal) as cm:
            self.apply(self.doc)
        self.assertIn(B580, cm.exception.what)
        self.assertIn("driver", cm.exception.what)

    def test_a_device_refuses_once_a_run_named_that_placement(self):
        (unit,) = [u for u in self.doc["units"] if u["encoder_unit_id"] == A4000]
        unit["device"] = "0000:09:00.0"
        with self.assertRaises(Refusal) as cm:
            self.apply(self.doc)
        self.assertIn("device", cm.exception.what)
        self.assertIn("run row", cm.exception.what)


if __name__ == "__main__":
    unittest.main()
