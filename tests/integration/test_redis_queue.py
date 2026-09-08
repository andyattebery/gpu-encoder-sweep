#!/usr/bin/env python3
"""sweep/hub/queue.py: RedisQueue passes the same contract FakeQueue does, on a real Redis.

    SWEEP_TEST_REDIS=redis://127.0.0.1:6379/9 python3 -m unittest tests.integration.test_redis_queue
"""
import os
import time
import unittest

from tests.test_hub_queue import QueueContract

URL = os.environ.get("SWEEP_TEST_REDIS")


class Redis(QueueContract, unittest.TestCase):
    TTL = 1     # a real clock: the heartbeat test sleeps past it

    @classmethod
    def setUpClass(cls):
        if not URL:
            raise unittest.SkipTest("set SWEEP_TEST_REDIS to the redis:// URL of a database this suite may flush")

    def make_queue(self):
        from sweep.hub.queue import RedisQueue
        q = RedisQueue(URL)
        q.flush_for_tests()
        return q

    def advance(self, seconds):
        time.sleep(seconds)


if __name__ == "__main__":
    unittest.main()
