#!/usr/bin/env python3
"""sweep/hub/auth.py: the operator's token opens the operator's routes and nothing else; an agent's token opens the agent
routes for its own host and nothing else; with no token configured the hub is open (the tests' hub).

    python3 -m unittest tests.test_hub_auth
"""
import json
import os
import pathlib
import tempfile
import unittest

from sweep.hub import auth
from tests.hub_helpers import client_for, fixture_store

HOST = {"host": "htpc-02", "ssh_host": "htpc-02", "os": "linux", "machine": "htpc-02", "work_root": "/data/sweep", "share_root": "/mnt/nas-01/sweep", "ffmpeg": "/ffmpeg/ffmpeg"}


def bearer(token):
    return {"Authorization": f"Bearer {token}"}


class Tokens(unittest.TestCase):
    def setUp(self):
        self.store = fixture_store()
        self.addCleanup(self.store.close)
        self.client = client_for(self.store, token="op", agent_tokens={"media-01": "a1", "eta": "a2"})

    def test_an_agent_acts_for_its_own_host_only(self):
        self.assertEqual(self.client.post("/agents/media-01/claim", json={"block_s": 0}, headers=bearer("a1")).status_code, 204)
        r = self.client.post("/agents/media-01/claim", json={"block_s": 0}, headers=bearer("a2"))
        self.assertEqual((r.status_code, r.text), (401, "REFUSING: the token presented is eta's, not media-01's -- an agent acts for its own host only"))
        r = self.client.get("/runs/b580-qsv-av1/abandon", headers=bearer("a2"))                    # a media-01 run
        self.assertEqual(r.status_code, 401)
        self.assertEqual(self.client.get("/runs/b580-qsv-av1/abandon", headers=bearer("a1")).status_code, 200)

    def test_an_agents_token_opens_no_operator_route(self):
        r = self.client.post("/catalogue/add-host", json=HOST, headers=bearer("a1"))
        self.assertEqual((r.status_code, r.text), (401, "REFUSING: an agent's token does not open POST /catalogue/add-host -- the operator's token, SWEEP_TOKEN on the laptop, does"))
        self.assertEqual(self.client.get("/runs/status", headers=bearer("a1")).status_code, 401)

    def test_the_operators_token_opens_no_agent_route(self):
        r = self.client.post("/agents/media-01/claim", json={"block_s": 0}, headers=bearer("op"))
        self.assertEqual((r.status_code, r.text), (401, "REFUSING: POST /agents/media-01/claim takes an agent's token -- set SWEEP_TOKEN on the agent to the token the hub holds for its host"))
        self.assertEqual(self.client.post("/catalogue/add-host", json=HOST, headers=bearer("op")).status_code, 200)

    def test_no_or_wrong_token_is_refused_everywhere(self):
        self.assertEqual(self.client.get("/runs/status").status_code, 401)
        self.assertEqual(self.client.post("/agents/media-01/claim", json={"block_s": 0}, headers=bearer("wrong")).status_code, 401)
        self.assertEqual(self.client.get("/runs/status", headers=bearer("wrong")).text, "REFUSING: no valid bearer token -- pass --token, or set SWEEP_TOKEN, to the hub's")

    def test_open_when_nothing_is_configured(self):
        client = client_for(self.store)
        self.assertEqual(client.get("/runs/status").status_code, 200)
        self.assertEqual(client.post("/agents/media-01/claim", json={"block_s": 0}).status_code, 204)


class Loading(unittest.TestCase):
    def test_tokens_come_from_the_environment_and_a_file_never_the_store(self):
        path = pathlib.Path(tempfile.mkdtemp()) / "agent-tokens.json"
        path.write_text(json.dumps({"media-01": "a1", "eta": "a2"}))
        tokens = auth.load_tokens({"SWEEP_TOKEN": "op", "SWEEP_AGENT_TOKENS": str(path)})
        self.assertEqual((tokens.operator, tokens.agents), ("op", {"media-01": "a1", "eta": "a2"}))
        self.assertEqual(auth.load_tokens({}), auth.Tokens(None, {}))
        with self.assertRaises(SystemExit) as cm:
            auth.load_tokens({"SWEEP_AGENT_TOKENS": "/no/such/file.json"})
        self.assertTrue(str(cm.exception).startswith("REFUSING: SWEEP_AGENT_TOKENS names /no/such/file.json, which does not exist -- "))

    def test_actor_of_a_bearer(self):
        tokens = auth.Tokens("op", {"media-01": "a1"})
        self.assertEqual((tokens.actor("Bearer op"), tokens.actor("Bearer a1"), tokens.actor("Bearer x"), tokens.actor(None)), (("operator", None), ("agent", "media-01"), None, None))
        self.assertEqual(auth.Tokens(None, {}).configured, False)


if __name__ == "__main__":
    unittest.main()
