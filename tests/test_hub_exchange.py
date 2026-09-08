#!/usr/bin/env python3
"""sweep/hub/exchange.py: the share and the work roots in each host's spelling, a runtime's view of another's work root
on the same machine, the transfer sha, and the record of a publish -- written once, verified at the hub's pool path.

    python3 -m unittest tests.test_hub_exchange
"""
import hashlib
import pathlib
import tempfile
import unittest

from sweep.hub import exchange
from sweep.hub.refusals import Refusal
from tests.hub_helpers import fixture_store


def host_row(store, host):
    return next(r for r in store.rows("host") if r["host"] == host)


class Paths(unittest.TestCase):
    def setUp(self):
        self.store = fixture_store()
        self.addCleanup(self.store.close)

    def test_share_paths_are_relative_forward_slashed_and_two_kinds(self):
        self.assertEqual(exchange.share_path("enc", run_id="encode-x-1", cell_key="abc"), "runs/encode-x-1/enc/abc.mkv")
        self.assertEqual(exchange.share_path("cut", reference_set_id="stage-1080p", window_id="tng", cut_kind="source"),
                         "refsets/stage-1080p/tng.source.mkv")
        with self.assertRaises(ValueError):
            exchange.share_path("log", run_id="r")

    def test_spelling_follows_the_host(self):
        rel = "runs/r/enc/k.mkv"
        self.assertEqual(exchange.spell(host_row(self.store, "media-01"), rel), "/mnt/nas-01/sweep/runs/r/enc/k.mkv")
        self.assertEqual(exchange.spell(host_row(self.store, "eta"), rel), r"\\nas-01\sweep\runs\r\enc\k.mkv")
        self.assertEqual(exchange.work_path(host_row(self.store, "media-01"), rel), "/mnt/data/sweep/runs/r/enc/k.mkv")
        self.assertEqual(exchange.work_path(host_row(self.store, "eta"), rel), r"D:\sweep\runs\r\enc\k.mkv")

    def test_a_runtime_views_another_on_its_machine_through_local_view(self):
        rel = "runs/r/enc/k.mkv"
        eta, eta_wsl = host_row(self.store, "eta"), host_row(self.store, "eta-wsl")
        self.assertEqual(exchange.viewed_path(eta_wsl, eta, rel), "/mnt/d/sweep/runs/r/enc/k.mkv")
        media, score = host_row(self.store, "media-01"), host_row(self.store, "media-01-score")
        self.assertEqual(exchange.viewed_path(score, media, rel), "/mnt/data/sweep/runs/r/enc/k.mkv")
        with self.assertRaises(Refusal) as cm:
            exchange.viewed_path(score, eta, rel)                     # another machine: the share is the way
        self.assertEqual(str(cm.exception), "REFUSING: media-01-score cannot see eta's work root: it is on machine media-01, eta on eta -- publish to the share and pull")
        with self.assertRaises(Refusal):
            exchange.viewed_path(media, score, rel)                   # media-01 has no local_view

    def test_a_windows_owner_seen_from_wsl_drops_the_drive(self):
        eta, eta_wsl = host_row(self.store, "eta"), host_row(self.store, "eta-wsl")
        self.assertEqual(exchange.viewed_path(eta_wsl, eta, "refsets/s/tng.reference.mkv"), "/mnt/d/sweep/refsets/s/tng.reference.mkv")


class Transfers(unittest.TestCase):
    def setUp(self):
        self.store = fixture_store()
        self.addCleanup(self.store.close)
        self.share = pathlib.Path(tempfile.mkdtemp())
        (self.share / "runs" / "b580-qsv-av1" / "enc").mkdir(parents=True)
        self.file = self.share / "runs" / "b580-qsv-av1" / "enc" / "c-a30-tng.mkv"
        self.file.write_bytes(b"x" * 100000)
        self.sha = hashlib.sha256(b"x" * 100000).hexdigest()

    def test_sha256_file_streams_the_whole_file(self):
        self.assertEqual(exchange.sha256_file(self.file), self.sha)
        self.assertEqual(exchange.sha256_file(self.file, buf=7), self.sha)

    def test_verify_arrival_checks_size_and_sha_at_the_pool_path(self):
        rel = "runs/b580-qsv-av1/enc/c-a30-tng.mkv"
        exchange.verify_arrival(self.share, rel, 100000, self.sha)
        with self.assertRaises(Refusal) as cm:
            exchange.verify_arrival(self.share, rel, 100000, "0" * 64)
        self.assertEqual(str(cm.exception), f"REFUSING: {rel} differs after the copy (sha {self.sha[:12]} on the share, 000000000000 before it) -- the transfer is corrupt; publish again")
        with self.assertRaises(Refusal) as cm:
            exchange.verify_arrival(self.share, rel, 99999, self.sha)
        self.assertIn("bytes", str(cm.exception))
        with self.assertRaises(Refusal) as cm:
            exchange.verify_arrival(self.share, "runs/b580-qsv-av1/enc/nope.mkv", 1, self.sha)
        self.assertEqual(str(cm.exception), "REFUSING: runs/b580-qsv-av1/enc/nope.mkv is not on the share -- the agent's copy did not arrive; publish again")

    def test_record_publish_is_write_once(self):
        rel = "runs/b580-qsv-av1/enc/c-a30-tng.mkv"
        with self.store.transaction() as conn:
            exchange.record_publish(conn, rel, "media-01", 100000, self.sha, "2026-09-05T10:00", run_id="b580-qsv-av1", cell_key="c-a30-tng")
            exchange.record_publish(conn, rel, "media-01", 100000, self.sha, "2026-09-05T10:05", run_id="b580-qsv-av1", cell_key="c-a30-tng")   # identical: a no-op
        rows = [r for r in self.store.rows("published") if r["path"] == rel]
        self.assertEqual([r["published_at"] for r in rows], ["2026-09-05T10:00"])
        with self.assertRaises(Refusal) as cm:
            with self.store.transaction() as conn:
                exchange.record_publish(conn, rel, "media-01", 100001, self.sha, "2026-09-05T10:10", run_id="b580-qsv-av1", cell_key="c-a30-tng")
        self.assertEqual(str(cm.exception), f"REFUSING: the share already holds a different {rel} -- a published file is never overwritten; publish under a new run, or remove it from the share by hand")

    def test_record_publish_of_a_cut(self):
        with self.store.transaction() as conn:
            exchange.record_publish(conn, "refsets/stage-1080p/tng.reference.mkv", "eta-wsl", 2000000000, self.sha, "2026-09-05T11:00", cut_id="tng.ref")
        row = next(r for r in self.store.rows("published") if r["cut_id"] == "tng.ref")
        self.assertEqual((row["by_host"], row["run_id"], row["cell_key"]), ("eta-wsl", None, None))


if __name__ == "__main__":
    unittest.main()
