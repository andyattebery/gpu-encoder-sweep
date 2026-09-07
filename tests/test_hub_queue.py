#!/usr/bin/env python3
"""sweep/hub/queue.py: FakeQueue, the in-memory queue M1 runs on -- FIFO per host, a restarted agent gets its unacked
entry back first, an ack of an entry it never claimed is refused.

    python3 -m unittest tests.test_hub_queue
"""
import unittest

from sweep.hub.queue import Entry, FakeQueue
from sweep.hub.refusals import Refusal


class Fake(unittest.TestCase):
    def setUp(self):
        self.q = FakeQueue()

    def test_claim_is_fifo_per_host(self):
        first = self.q.enqueue("media-01", "r1", {"cells": 1})
        second = self.q.enqueue("media-01", "r2", {"cells": 2})
        self.assertEqual((first, second), ("1", "2"))
        e = self.q.claim("media-01")
        self.assertEqual(e, Entry("1", "media-01", "r1", {"cells": 1}))
        self.q.ack("media-01", "1")
        self.assertEqual(self.q.claim("media-01").run_id, "r2")

    def test_claim_on_an_empty_queue_returns_none(self):
        self.assertIsNone(self.q.claim("media-01"))

    def test_reclaim_returns_the_unacked_entry_first(self):
        self.q.enqueue("media-01", "r1", {})
        self.q.enqueue("media-01", "r2", {})
        self.assertEqual(self.q.claim("media-01").run_id, "r1")
        self.assertEqual(self.q.claim("media-01").run_id, "r1")     # the agent restarted: the same entry, not the next
        self.q.ack("media-01", "1")
        self.assertEqual(self.q.claim("media-01").run_id, "r2")

    def test_ack_of_an_unknown_entry_is_refused(self):
        self.q.enqueue("media-01", "r1", {})
        with self.assertRaises(Refusal) as cm:
            self.q.ack("media-01", "1")                                # queued, never claimed
        self.assertEqual(str(cm.exception), "REFUSING: entry '1' is not claimed by media-01 -- claim before ack")
        self.q.claim("media-01")
        with self.assertRaises(Refusal):
            self.q.ack("eta", "1")                                     # another host

    def test_hosts_are_isolated(self):
        self.q.enqueue("media-01", "r1", {})
        self.q.enqueue("eta", "r2", {})
        self.assertEqual(self.q.claim("eta").run_id, "r2")
        self.assertEqual(self.q.claim("media-01").run_id, "r1")

    def test_pending_counts_queued_and_claimed(self):
        self.q.enqueue("media-01", "r1", {})
        self.q.enqueue("media-01", "r2", {})
        self.q.claim("media-01")
        self.assertEqual([e.run_id for e in self.q.pending("media-01")], ["r1", "r2"])
        self.q.ack("media-01", "1")
        self.assertEqual([e.run_id for e in self.q.pending("media-01")], ["r2"])
        self.assertEqual(self.q.pending("eta"), [])


if __name__ == "__main__":
    unittest.main()
