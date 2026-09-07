#!/usr/bin/env python3
"""sweep/cli: every verb a subcommand whose flags are the body's fields, proven against the app's OpenAPI document;
the hub's text is printed as is, and the exit code says refused (1), ok (0) or unreachable (2).

    python3 -m unittest tests.test_cli
"""
import contextlib
import io
import json
import pathlib
import tempfile
import unittest

import httpx

from sweep import cli
from sweep.hub.app import create_app
from sweep.hub.store import Store


def run(argv, handler):
    """(exit code, stdout, stderr, the requests the transport saw) for one CLI invocation against a mock hub."""
    seen = []

    def respond(request):
        seen.append(request)
        return handler(request)

    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = cli.main(["--hub", "http://hub.test"] + argv, transport=httpx.MockTransport(respond))
    return rc, out.getvalue(), err.getvalue(), seen


class Exits(unittest.TestCase):
    def test_refusing_body_exits_1_and_prints_it(self):
        rc, out, _, _ = run(["add-host", "--host", "h"], lambda r: httpx.Response(422, text="REFUSING: add-host needs 'os' -- give --os"))
        self.assertEqual((rc, out), (1, "REFUSING: add-host needs 'os' -- give --os\n"))

    def test_ok_body_exits_0(self):
        rc, out, _, seen = run(["add-host", "--host", "h", "--os", "linux"], lambda r: httpx.Response(200, json={"ok": True}))
        self.assertEqual((rc, out), (0, '{"ok":true}\n'))
        self.assertEqual((seen[0].method, str(seen[0].url)), ("POST", "http://hub.test/catalogue/add-host"))
        self.assertEqual(json.loads(seen[0].content), {"host": "h", "os": "linux"})

    def test_transport_error_exits_2(self):
        def down(request):
            raise httpx.ConnectError("connection refused", request=request)
        rc, _, err, _ = run(["add-host", "--host", "h"], down)
        self.assertEqual(rc, 2)
        self.assertIn("http://hub.test", err)

    def test_token_is_sent_as_a_bearer(self):
        _, _, _, seen = run(["--token", "s3cret", "add-host", "--host", "h"], lambda r: httpx.Response(200, json={"ok": True}))
        self.assertEqual(seen[0].headers["authorization"], "Bearer s3cret")


class Flags(unittest.TestCase):
    def test_bool_int_float_and_list_flags_are_typed(self):
        argv = ["add-lane", "--lane", "l", "--has-content", "true", "--steps", "probe,remux", "--input-width-max", "1920",
                "--score-target", "78.5", "--min-content-rate", "null"]
        _, _, _, seen = run(argv, lambda r: httpx.Response(200, json={"ok": True}))
        self.assertEqual(json.loads(seen[0].content), {"lane": "l", "has_content": True, "steps": ["probe", "remux"],
                                                        "input_width_max": 1920, "score_target": 78.5, "min_content_rate": None})

    def test_json_flags_carry_nested_bodies(self):
        argv = ["add-setting", "--setting-id", "s", "--scope", '[{"encoder_unit_id": "u", "applies": true, "default_is_measured": false}]']
        _, _, _, seen = run(argv, lambda r: httpx.Response(200, json={"ok": True}))
        self.assertEqual(json.loads(seen[0].content)["scope"], [{"encoder_unit_id": "u", "applies": True, "default_is_measured": False}])

    def test_from_file_supplies_the_body_and_flags_override(self):
        path = pathlib.Path(tempfile.mkdtemp()) / "setting.json"
        path.write_text(json.dumps({"setting_id": "s", "flag": "-s", "kind": "option", "roles": ["preset"]}))
        _, _, _, seen = run(["add-setting", "--from", str(path), "--kind", "ordinal"], lambda r: httpx.Response(200, json={"ok": True}))
        self.assertEqual(json.loads(seen[0].content), {"setting_id": "s", "flag": "-s", "kind": "ordinal", "roles": ["preset"]})


class VerbsMatchTheApp(unittest.TestCase):
    def test_every_verb_has_a_subparser_and_the_flags_are_the_fields(self):
        store = Store()
        self.addCleanup(store.close)
        spec = create_app(store, None).openapi()
        expected = {}
        for path, ops in spec["paths"].items():
            for method, op in ops.items():
                name = path.rsplit("/", 1)[-1]
                fields = []
                body = op.get("requestBody")
                if body:
                    ref = body["content"]["application/json"]["schema"]["$ref"].rsplit("/", 1)[-1]
                    fields = list(spec["components"]["schemas"][ref]["properties"])
                expected[name] = (method.upper(), path, fields)
        self.assertEqual(set(cli.VERBS), set(expected), "the CLI's verbs are the app's, no more and no fewer")
        for name, verb in cli.VERBS.items():
            with self.subTest(verb=name):
                self.assertEqual((verb.method, verb.path, [f for f, _ in verb.flags]), expected[name])

    def test_help_lists_every_verb(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit):
            cli.main(["--help"])
        for name in cli.VERBS:
            self.assertIn(name, out.getvalue())


if __name__ == "__main__":
    unittest.main()
