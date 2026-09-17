#!/usr/bin/env python3
"""sweep/hub/queue.py: the queue's contract, proven on FakeQueue here and on RedisQueue in tests/integration -- FIFO per
host, a restarted agent gets its unacked entry back first, an ack of an entry it never claimed is refused, heartbeats
expire, a channel replays what was published until a final payload, a publish job is an entry of its own kind.

    python3 -m unittest tests.test_hub_queue
"""
import unittest

from sweep.hub import queue as q_mod
from sweep.hub.queue import Entry, FakeQueue
from sweep.hub.refusals import Refusal


class QueueContract:
    """The tests every Queue passes. A subclass gives make_queue(), TTL (seconds a heartbeat lives here) and advance(s)."""
    TTL = 90
    BLOCK_S = 1          # the Redis subclass raises this past redis-py's own socket timeout, where the case bites

    def setUp(self):
        self.q = self.make_queue()

    def test_claim_is_fifo_per_host(self):
        first = self.q.enqueue("media-01", "r1", {"cells": 1})
        second = self.q.enqueue("media-01", "r2", {"cells": 2})
        self.assertNotEqual(first, second)
        e = self.q.claim("media-01")
        self.assertEqual(e, Entry(first, "media-01", "r1", {"cells": 1}))
        self.assertEqual(e.kind, "run")
        self.q.ack("media-01", first)
        self.assertEqual(self.q.claim("media-01").run_id, "r2")

    def test_claim_on_an_empty_queue_returns_none(self):
        self.assertIsNone(self.q.claim("media-01"))
        self.assertIsNone(self.q.claim("media-01", block_s=0))

    def test_a_blocking_claim_on_an_empty_queue_returns_none(self):
        # the idle agent's normal call, and the case no test made before this one: block_s=0 takes the branch that
        # asks Redis for no BLOCK at all, so a blocking read was never issued anywhere in the suite while the live
        # hub answered 500 to every idle poll
        self.assertIsNone(self.q.claim("media-01", block_s=self.BLOCK_S))

    def test_reclaim_returns_the_unacked_entry_first(self):
        first = self.q.enqueue("media-01", "r1", {})
        self.q.enqueue("media-01", "r2", {})
        self.assertEqual(self.q.claim("media-01").run_id, "r1")
        self.assertEqual(self.q.claim("media-01").run_id, "r1")     # the agent restarted: the same entry, not the next
        self.q.ack("media-01", first)
        self.assertEqual(self.q.claim("media-01").run_id, "r2")

    def test_ack_of_an_unknown_entry_is_refused(self):
        first = self.q.enqueue("media-01", "r1", {})
        with self.assertRaises(Refusal) as cm:
            self.q.ack("media-01", first)                              # queued, never claimed
        self.assertEqual(str(cm.exception), f"REFUSING: entry {first!r} is not claimed by media-01 -- claim before ack")
        self.q.claim("media-01")
        with self.assertRaises(Refusal):
            self.q.ack("eta", first)                                   # another host

    def test_hosts_are_isolated(self):
        self.q.enqueue("media-01", "r1", {})
        self.q.enqueue("eta", "r2", {})
        self.assertEqual(self.q.claim("eta").run_id, "r2")
        self.assertEqual(self.q.claim("media-01").run_id, "r1")

    def test_pending_counts_queued_and_claimed(self):
        first = self.q.enqueue("media-01", "r1", {})
        self.q.enqueue("media-01", "r2", {})
        self.q.claim("media-01")
        self.assertEqual([e.run_id for e in self.q.pending("media-01")], ["r1", "r2"])
        self.q.ack("media-01", first)
        self.assertEqual([e.run_id for e in self.q.pending("media-01")], ["r2"])
        self.assertEqual(self.q.pending("eta"), [])

    def test_publish_entry_carries_its_kind(self):
        # a publish job is a queue entry, not a run: no run id, the files in its plan
        self.q.enqueue("eta-wsl", None, {"files": [{"relative": "refsets/s/tng.reference.mkv"}]}, kind="publish")
        e = self.q.claim("eta-wsl")
        self.assertEqual((e.kind, e.run_id, e.plan["files"][0]["relative"]), ("publish", None, "refsets/s/tng.reference.mkv"))
        self.assertEqual(Entry("9", "eta-wsl", None, {}).kind, "run")      # the M1 positional form still means a run

    def test_beat_expires_by_the_clock(self):
        self.assertIsNone(self.q.pulse("media-01"))
        self.q.beat("media-01", {"run_id": "r1", "cells_done": 1, "cells_total": 2}, self.TTL)
        self.assertEqual(self.q.pulse("media-01")["cells_done"], 1)
        self.advance(self.TTL + 1)
        self.assertIsNone(self.q.pulse("media-01"))                     # a silent agent is reported silent, not zero

    def test_listen_replays_until_final(self):
        heard = self.q.listen("r1", timeout_s=2)                        # subscribed before anything is published
        self.q.publish("r1", {"state": "running"})
        self.q.publish("r2", {"state": "running"})                      # another run's channel
        self.q.publish("r1", {"state": "complete", "final": True})
        self.q.publish("r1", {"state": "after"})                        # never delivered: the channel ended
        self.assertEqual(list(heard), [{"state": "running"}, {"state": "complete", "final": True}])


class Blocking(unittest.TestCase):
    def test_the_socket_timeout_exceeds_the_longest_block_we_ask_for(self):
        # redis-py's default socket timeout is 5s and a blocking command does not raise it, so BLOCK 30000 gave up
        # at 5s every time. These two have to stay apart or an expired socket stops meaning "the queue is gone".
        self.assertGreater(q_mod.SOCKET_TIMEOUT_S, q_mod.BLOCK_CEILING_S)


class WhenTheQueueIsGone(unittest.TestCase):
    """A Redis that does not answer refused nothing before this: grep found no Redis error handling in sweep/hub at
    all, so a dead queue reached the agent as a 500 and a traceback. Needs no server -- nothing listens on port 1."""

    def test_every_call_refuses_by_name_and_never_quotes_the_url(self):
        from sweep.hub.queue import RedisQueue
        q = RedisQueue("redis://127.0.0.1:1/9")
        calls = {"enqueue": lambda: q.enqueue("media-01", "r1", {}), "claim": lambda: q.claim("media-01"),
                 "ack": lambda: q.ack("media-01", "1-1"), "pending": lambda: q.pending("media-01"),
                 "beat": lambda: q.beat("media-01", {}, 90), "pulse": lambda: q.pulse("media-01"),
                 "publish": lambda: q.publish("r1", {})}
        for name, call in calls.items():
            with self.subTest(call=name):
                with self.assertRaises(Refusal) as cm:
                    call()
                self.assertIn("127.0.0.1:1", str(cm.exception))
                self.assertNotIn("redis://", str(cm.exception))      # a URL can carry a password; host:port cannot


class Fake(QueueContract, unittest.TestCase):
    def make_queue(self):
        self.now = 0.0
        return FakeQueue(clock=lambda: self.now)

    def advance(self, seconds):
        self.now += seconds

    def test_ids_are_sequential(self):
        self.assertEqual((self.q.enqueue("media-01", "r1", {}), self.q.enqueue("media-01", "r2", {})), ("1", "2"))


if __name__ == "__main__":
    unittest.main()
