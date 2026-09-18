#!/usr/bin/env python3
"""sweep/hub/exchange.py: the exchange paths and the work roots in each host's spelling, a runtime's view of another's
work root on the same machine, the transfer sha, the receiver that proves a stream as it lands, and the record of a
publish -- written once.

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

    def test_check_path_admits_the_two_kinds_and_nothing_else(self):
        exchange.check_path("runs/encode-x-1/enc/abc.mkv")
        exchange.check_path("refsets/stage-1080p/tng.source.mkv")
        for bad in ("runs/../etc/passwd", "runs/r/enc/k.mkv/..", "refsets/s/tng.mkv", "logs/r.txt", "/runs/r/enc/k.mkv", "runs/r/enc/k.mp4", ""):
            with self.subTest(path=bad):
                with self.assertRaises(Refusal) as cm:
                    exchange.check_path(bad)
                self.assertEqual(str(cm.exception), f"REFUSING: {bad} is not an exchange path -- the exchange holds "
                                                    "runs/<run_id>/enc/<cell_key>.mkv and refsets/<reference_set_id>/<window_id>.<kind>.mkv")

    def test_work_paths_follow_the_host(self):
        rel = "runs/r/enc/k.mkv"
        self.assertEqual(exchange.work_path(host_row(self.store, "media-01"), rel), "/mnt/data/sweep/runs/r/enc/k.mkv")
        self.assertEqual(exchange.work_path(host_row(self.store, "eta"), rel), r"D:\sweep\runs\r\enc\k.mkv")

    def test_a_runtime_views_another_on_its_machine_through_local_view(self):
        rel = "runs/r/enc/k.mkv"
        eta, eta_wsl = host_row(self.store, "eta"), host_row(self.store, "eta-wsl")
        self.assertEqual(exchange.viewed_path(eta_wsl, eta, rel), "/mnt/d/sweep/runs/r/enc/k.mkv")
        media, score = host_row(self.store, "media-01"), host_row(self.store, "media-01-score")
        self.assertEqual(exchange.viewed_path(score, media, rel), "/mnt/data/sweep/runs/r/enc/k.mkv")
        with self.assertRaises(Refusal) as cm:
            exchange.viewed_path(score, eta, rel)                     # another machine: the exchange is the way
        self.assertEqual(str(cm.exception), "REFUSING: media-01-score cannot see eta's work root: it is on machine media-01, eta on eta -- publish and pull")
        with self.assertRaises(Refusal):
            exchange.viewed_path(media, score, rel)                   # media-01 has no local_view

    def test_a_windows_owner_seen_from_wsl_drops_the_drive(self):
        eta, eta_wsl = host_row(self.store, "eta"), host_row(self.store, "eta-wsl")
        self.assertEqual(exchange.viewed_path(eta_wsl, eta, "refsets/s/tng.reference.mkv"), "/mnt/d/sweep/refsets/s/tng.reference.mkv")


class Transfers(unittest.TestCase):
    def setUp(self):
        self.store = fixture_store()
        self.addCleanup(self.store.close)
        self.root = pathlib.Path(tempfile.mkdtemp())
        self.rel = "runs/b580-qsv-av1/enc/c-a30-tng.mkv"
        self.data = b"x" * 100000
        self.sha = hashlib.sha256(self.data).hexdigest()

    def receive(self, chunks):
        r = exchange.Receiver(self.root, self.rel)
        for c in chunks:
            r.write(c)
        return r

    def test_sha256_file_streams_the_whole_file(self):
        f = self.root / "f"
        f.write_bytes(self.data)
        self.assertEqual(exchange.sha256_file(f), self.sha)
        self.assertEqual(exchange.sha256_file(f, buf=7), self.sha)

    def test_a_receiver_lands_the_stream_under_a_temp_name_and_commits_it_into_place(self):
        r = self.receive([self.data[:40000], self.data[40000:]])
        target = self.root / "runs" / "b580-qsv-av1" / "enc" / "c-a30-tng.mkv"
        self.assertFalse(target.exists())
        r.finish(100000, self.sha)
        r.commit()
        self.assertEqual(target.read_bytes(), self.data)
        self.assertEqual(sorted(p.name for p in target.parent.iterdir()), ["c-a30-tng.mkv"])       # nothing temporary left beside it

    def test_two_receivers_of_one_path_do_not_share_a_temp_file(self):
        a, b = exchange.Receiver(self.root, self.rel), exchange.Receiver(self.root, self.rel)
        self.assertNotEqual(a.temp, b.temp)
        a.discard()
        b.discard()

    def test_a_short_transfer_is_refused_and_leaves_nothing(self):
        r = self.receive([self.data[:99999]])
        with self.assertRaises(Refusal) as cm:
            r.finish(100000, self.sha)
        self.assertEqual(str(cm.exception), f"REFUSING: {self.rel} is 99999 bytes at the hub, 100000 before the send -- the transfer is short; publish again")
        self.assertEqual(list(self.root.rglob("*.mkv")), [])
        self.assertEqual([p for p in self.root.rglob("*") if p.is_file()], [])

    def test_a_corrupt_transfer_is_refused_and_leaves_nothing(self):
        r = self.receive([self.data])
        with self.assertRaises(Refusal) as cm:
            r.finish(100000, "0" * 64)
        self.assertEqual(str(cm.exception), f"REFUSING: {self.rel} differs in transit (sha {self.sha[:12]} at the hub, 000000000000 before the send) -- the transfer is corrupt; publish again")
        self.assertEqual([p for p in self.root.rglob("*") if p.is_file()], [])

    def test_a_commit_replaces_an_identical_file_atomically(self):
        target = self.root / "runs" / "b580-qsv-av1" / "enc" / "c-a30-tng.mkv"
        target.parent.mkdir(parents=True)
        target.write_bytes(self.data)
        r = self.receive([self.data])
        r.finish(100000, self.sha)
        r.commit()
        self.assertEqual(target.read_bytes(), self.data)
        self.assertEqual(sorted(p.name for p in target.parent.iterdir()), ["c-a30-tng.mkv"])

    def test_record_publish_is_write_once(self):
        with self.store.transaction() as conn:
            exchange.record_publish(conn, self.rel, "media-01", 100000, self.sha, "2026-09-05T10:00", run_id="b580-qsv-av1", cell_key="c-a30-tng")
            exchange.record_publish(conn, self.rel, "media-01", 100000, self.sha, "2026-09-05T10:05", run_id="b580-qsv-av1", cell_key="c-a30-tng")   # identical: a no-op
        rows = [r for r in self.store.rows("published") if r["path"] == self.rel]
        self.assertEqual([r["published_at"] for r in rows], ["2026-09-05T10:00"])
        with self.assertRaises(Refusal) as cm:
            with self.store.transaction() as conn:
                exchange.record_publish(conn, self.rel, "media-01", 100001, self.sha, "2026-09-05T10:10", run_id="b580-qsv-av1", cell_key="c-a30-tng")
        self.assertEqual(str(cm.exception), f"REFUSING: the hub already holds a different {self.rel} -- a published file is never overwritten; publish under a new run, or remove it from the exchange by hand")

    def test_record_publish_of_a_cut(self):
        with self.store.transaction() as conn:
            exchange.record_publish(conn, "refsets/stage-1080p/tng.reference.mkv", "eta-wsl", 2000000000, self.sha, "2026-09-05T11:00", cut_id="tng.ref")
        row = next(r for r in self.store.rows("published") if r["cut_id"] == "tng.ref")
        self.assertEqual((row["by_host"], row["run_id"], row["cell_key"]), ("eta-wsl", None, None))


if __name__ == "__main__":
    unittest.main()
