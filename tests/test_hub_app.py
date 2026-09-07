#!/usr/bin/env python3
"""sweep/hub/app.py: a refusal is a 422 whose plain-text body starts REFUSING; an unknown or missing field is one too;
a bearer token guards every route when set.

    python3 -m unittest tests.test_hub_app
"""
import unittest

from sweep.hub import refusals
from tests.hub_helpers import client_for, fixture_store, post

HOST = {"host": "htpc-02", "ssh_host": "htpc-02", "os": "linux", "machine": "htpc-02", "work_root": "/data/sweep",
        "share_root": "/mnt/nas-01/sweep", "ffmpeg": "/ffmpeg/ffmpeg"}


class Handlers(unittest.TestCase):
    def setUp(self):
        self.store = fixture_store()
        self.addCleanup(self.store.close)
        self.client = client_for(self.store)

    def test_refusal_is_a_422_plain_text_body(self):
        self.assertEqual(post(self.client, "/catalogue/add-host", HOST), (200, '{"ok":true}'))
        status, text = post(self.client, "/catalogue/add-host", HOST)
        self.assertEqual(status, 422)
        self.assertTrue(text.startswith("REFUSING: host already has a row with that host -- "), text)
        r = self.client.post("/catalogue/add-host", json=HOST)
        self.assertTrue(r.headers["content-type"].startswith("text/plain"))

    def test_unknown_field_is_refused(self):
        status, text = post(self.client, "/catalogue/add-host", dict(HOST, devcie="x"))
        self.assertEqual(status, 422)
        self.assertEqual(text, "REFUSING: add-host does not take 'devcie' -- the fields are: host, ssh_host, os, machine, "
                               "work_root, share_root, ffmpeg, local_view, notes")

    def test_missing_field_is_refused(self):
        body = dict(HOST)
        del body["os"]
        self.assertEqual(post(self.client, "/catalogue/add-host", body), (422, "REFUSING: add-host needs 'os' -- give --os"))

    def test_a_field_of_the_wrong_type_is_refused(self):
        status, text = post(self.client, "/catalogue/add-host", dict(HOST, host=7))
        self.assertEqual(status, 422)
        self.assertTrue(text.startswith("REFUSING: add-host: host "), text)
        self.assertTrue(text.endswith(" -- give --host as the body schema says; GET /openapi.json describes it"), text)

    def test_a_body_that_is_not_json_is_refused(self):
        r = self.client.post("/catalogue/add-host", content=b"not json", headers={"content-type": "application/json"})
        self.assertEqual((r.status_code, r.text), (422, "REFUSING: add-host: the body is not a JSON object -- post one; the CLI builds it from the flags"))

    def test_bearer_token_is_required_when_set(self):
        client = client_for(self.store, token="s3cret")
        r = client.post("/catalogue/add-host", json=HOST)
        self.assertEqual((r.status_code, r.text), (401, "REFUSING: no valid bearer token -- pass --token, or set SWEEP_TOKEN, to the hub's"))
        r = client.post("/catalogue/add-host", json=HOST, headers={"Authorization": "Bearer wrong"})
        self.assertEqual(r.status_code, 401)
        r = client.post("/catalogue/add-host", json=HOST, headers={"Authorization": "Bearer s3cret"})
        self.assertEqual((r.status_code, r.text), (200, '{"ok":true}'))


class StatusAndCheck(unittest.TestCase):
    def setUp(self):
        self.store = fixture_store()
        self.addCleanup(self.store.close)
        self.client = client_for(self.store)

    def test_status_lists_runs_and_blocked_hosts(self):
        self.client.app.state.queue.enqueue("media-01", "r-next", {})
        r = self.client.get("/runs/status")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        run = [x for x in body["runs"] if x["run_id"] == "b580-qsv-av1"][0]
        self.assertEqual((run["host"], run["stage"], run["state"], run["planned_total"], run["scored"], run["failed"]),
                         ("media-01", "encode", "complete", 89, 88, 1))
        self.assertIn({"host": "htpc-01", "blocked": "mount the sweep tree into tdarr-node, point the work root at it, use /ffmpeg/ffmpeg, then clear this"}, body["hosts"])
        self.assertEqual((body["queue"]["media-01"], body["queue"]["eta"]), (1, 0))

    def test_check_is_ok_on_a_clean_store(self):
        r = self.client.get("/analysis/check")
        self.assertEqual((r.status_code, r.text), (200, '{"ok":true}'))

    def test_check_refuses_with_the_first_firing_check_and_lists_the_rest(self):
        self.store.conn.executescript("DELETE FROM scorer; INSERT INTO routing_exclusion VALUES ('m4-ipad-le1080p-sdr', 'media-01', 'x')")
        r = self.client.get("/analysis/check")
        self.assertEqual(r.status_code, 422)
        lines = r.text.splitlines()
        self.assertTrue(lines[0].startswith("REFUSING: x_routed_and_excluded: "), lines[0])
        self.assertTrue(lines[0].endswith(" -- " + self.store.checks["x_routed_and_excluded"]["fix"]), lines[0])
        self.assertTrue(lines[1].startswith("x_score_run_host_without_scorer: "), lines[1])
        self.assertEqual(len(lines), 2)


class ValidationRefusals(unittest.TestCase):
    def test_nested_field_names_its_path_and_its_flag(self):
        errors = [{"type": "missing", "loc": ("body", "scope", 0, "encoder_unit_id"), "msg": "Field required"}]
        r = refusals.validation_refusal("add-setting", errors, ["setting_id", "scope"])
        self.assertEqual(str(r), "REFUSING: add-setting needs 'scope[0].encoder_unit_id' -- give --scope")


if __name__ == "__main__":
    unittest.main()
