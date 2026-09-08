#!/usr/bin/env python3
"""The agent protocol on a real Redis: the two-cell run and the killed agent's resume, with RedisQueue where the unit
tests use FakeQueue. The heartbeat TTL is real time here, so the killed agent's run fails after a short TTL.

    SWEEP_TEST_REDIS=redis://127.0.0.1:6379/9 python3 -m unittest tests.integration.test_node_agent_redis
"""
import json
import os
import time
import unittest
import unittest.mock

from sweep.hub import wait
from sweep.hub.api import agents as agents_api
from tests.hub_helpers import post
from tests.node_helpers import CLASS, UNIT, hub_and_agent, make_sample

URL = os.environ.get("SWEEP_TEST_REDIS")
VIEW = {"stage": "viewing", "content_class_id": CLASS, "encoder_unit_id": UNIT, "host": "enc",
        "cells": [{"window_id": "tng", "settings": {"qsv.q": "30", "qsv.preset": "4", "qsv.b_strategy": "0"}},
                  {"window_id": "parks", "settings": {"qsv.q": "34", "qsv.preset": "4", "qsv.b_strategy": "0"}}]}


class AgentOnRedis(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not URL:
            raise unittest.SkipTest("set SWEEP_TEST_REDIS to the redis:// URL of a database this suite may flush")

    def setUp(self):
        from sweep.hub.queue import RedisQueue
        q = RedisQueue(URL)
        q.flush_for_tests()
        self.p = hub_and_agent(queue=q)
        self.agent = make_sample(self.p)

    def plan(self, path, body):
        status, text = post(self.p.client, path, body)
        self.assertEqual(status, 200, text)
        return json.loads(text)

    def test_a_two_cell_run_completes_through_redis(self):
        run_id = self.plan("/runs/encode", VIEW)["run_id"]
        self.assertEqual(self.agent.serve_once(), run_id)
        self.assertEqual(self.p.run_row(run_id)["state"], "complete")
        self.assertEqual(self.p.queue.pending("enc"), [])

    def test_a_killed_agent_reads_failed_and_resumes_through_redis(self):
        run_id = self.plan("/runs/encode", VIEW)["run_id"]
        with unittest.mock.patch.object(agents_api, "TTL_S", 1):
            self.agent.serve_once(die_after=1)
        time.sleep(1.5)
        self.assertEqual(wait.sweep_once(self.p.store, self.p.queue), [run_id])
        fresh = self.p.agent("enc")
        fresh.start()
        self.assertEqual(fresh.serve_once(), run_id)
        self.assertEqual((self.p.run_row(run_id)["state"], fresh.cells_run), ("complete", 1))


if __name__ == "__main__":
    unittest.main()
