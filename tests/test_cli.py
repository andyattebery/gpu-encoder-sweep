#!/usr/bin/env python3
"""sweep/cli: every verb a subcommand whose flags are the body's fields, proven against the app's OpenAPI document;
the hub's text is printed as is, and the exit code says refused (1), ok (0) or unreachable (2).

    python3 -m unittest tests.test_cli
"""
import contextlib
import io
import json
import os
import pathlib
import re
import stat
import tempfile
import unittest

import httpx

from sweep import cli
from sweep.cli import config as cliconfig
from sweep.hub.app import create_app
from sweep.hub.store import Store


def run(argv, handler, env=None, hub=("--hub", "http://hub.test")):
    """(exit code, stdout, stderr, the requests the transport saw) for one CLI invocation against a mock hub."""
    seen = []

    def respond(request):
        seen.append(request)
        return handler(request)

    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = cli.main(list(hub) + argv, transport=httpx.MockTransport(respond), env=env)
    return rc, out.getvalue(), err.getvalue(), seen


def config_file(text, mode=0o600, name="env"):
    """A config file with a chosen mode, and an env that points at it."""
    d = pathlib.Path(tempfile.mkdtemp())
    p = d / name
    p.write_text(text)
    p.chmod(mode)
    return p, {"SWEEP_CONFIG": str(p)}


class ConfigFile(unittest.TestCase):
    """sweep/cli/config.py: where the hub and the token are kept when the environment does not carry them."""

    def test_the_file_supplies_hub_and_token_when_the_environment_does_not(self):
        _, env = config_file("SWEEP_HUB=https://hub.example\nSWEEP_TOKEN=s3cret\n")
        self.assertEqual(cliconfig.read(env), {"SWEEP_HUB": "https://hub.example", "SWEEP_TOKEN": "s3cret"})

    def test_comments_blank_lines_export_and_quotes_are_handled(self):
        _, env = config_file("""# the hub

export SWEEP_HUB="https://hub.example"
  SWEEP_TOKEN = 's3cret'   
""")
        self.assertEqual(cliconfig.read(env), {"SWEEP_HUB": "https://hub.example", "SWEEP_TOKEN": "s3cret"})

    def test_a_group_readable_file_is_refused_with_chmod_as_the_fix(self):
        p, env = config_file("SWEEP_HUB=https://hub.example\n", mode=0o644)
        with self.assertRaises(SystemExit) as cm:
            cliconfig.read(env)
        self.assertRegex(str(cm.exception), r"^REFUSING: .+ -- .+$")
        self.assertIn(str(p), str(cm.exception))
        self.assertIn(f"chmod 600 {p}", str(cm.exception))

    def test_an_unrecognised_key_is_refused_by_name(self):
        _, env = config_file("SWEEP_TOKN=s3cret\n")
        with self.assertRaises(SystemExit) as cm:
            cliconfig.read(env)
        self.assertIn("SWEEP_TOKN", str(cm.exception))
        self.assertIn("SWEEP_HUB", str(cm.exception))          # the fix names what is allowed

    def test_sweep_config_naming_a_missing_file_is_refused_but_a_missing_default_is_not(self):
        missing = pathlib.Path(tempfile.mkdtemp()) / "nope"
        with self.assertRaises(SystemExit) as cm:
            cliconfig.read({"SWEEP_CONFIG": str(missing)})
        self.assertIn(str(missing), str(cm.exception))
        self.assertEqual(cliconfig.read({"HOME": tempfile.mkdtemp()}), {})   # no file, no complaint

    def test_xdg_config_home_is_honoured_and_sweep_config_overrides_it(self):
        home, xdg = tempfile.mkdtemp(), tempfile.mkdtemp()
        self.assertEqual(cliconfig.path({"HOME": home}), pathlib.Path(home) / ".config/sweep/env")
        self.assertEqual(cliconfig.path({"HOME": home, "XDG_CONFIG_HOME": xdg}), pathlib.Path(xdg) / "sweep/env")
        self.assertEqual(cliconfig.path({"HOME": home, "XDG_CONFIG_HOME": xdg, "SWEEP_CONFIG": "/tmp/x"}), pathlib.Path("/tmp/x"))


class Precedence(unittest.TestCase):
    """The flag beats the environment beats the file, per value."""

    OK = staticmethod(lambda r: httpx.Response(200, json={"ok": True}))

    def test_the_file_is_used_when_the_environment_has_neither(self):
        _, env = config_file("SWEEP_HUB=https://from.file\nSWEEP_TOKEN=file-token\n")
        _, _, _, seen = run(["status"], self.OK, env=env, hub=())
        self.assertEqual(str(seen[0].url), "https://from.file/runs/status")
        self.assertEqual(seen[0].headers["authorization"], "Bearer file-token")

    def test_the_environment_beats_the_file(self):
        _, env = config_file("SWEEP_HUB=https://from.file\nSWEEP_TOKEN=file-token\n")
        env |= {"SWEEP_HUB": "https://from.env", "SWEEP_TOKEN": "env-token"}
        _, _, _, seen = run(["status"], self.OK, env=env, hub=())
        self.assertEqual(str(seen[0].url), "https://from.env/runs/status")
        self.assertEqual(seen[0].headers["authorization"], "Bearer env-token")

    def test_the_flag_beats_both(self):
        _, env = config_file("SWEEP_HUB=https://from.file\nSWEEP_TOKEN=file-token\n")
        env |= {"SWEEP_HUB": "https://from.env", "SWEEP_TOKEN": "env-token"}
        _, _, _, seen = run(["status"], self.OK, env=env, hub=("--hub", "https://from.flag", "--token", "flag-token"))
        self.assertEqual(str(seen[0].url), "https://from.flag/runs/status")
        self.assertEqual(seen[0].headers["authorization"], "Bearer flag-token")

    def test_the_hub_falls_back_to_localhost_and_no_token_sends_no_header(self):
        _, _, _, seen = run(["status"], self.OK, env={"HOME": tempfile.mkdtemp()}, hub=())
        self.assertEqual(str(seen[0].url), "http://127.0.0.1:8000/runs/status")
        self.assertNotIn("authorization", seen[0].headers)


def mode_of(p):
    return stat.S_IMODE(os.stat(p).st_mode)


class ConfigCommand(unittest.TestCase):
    """`sweep config` shows where each value came from; `--save` writes the file so the mode is not the operator's job."""

    def run_config(self, argv, env, hub=()):
        return run(["config"] + argv, lambda r: httpx.Response(200, json={}), env=env, hub=hub)

    def test_config_reports_each_value_and_its_source_without_printing_the_token(self):
        path, env = config_file("SWEEP_HUB=https://from.file\nSWEEP_TOKEN=s3cret-value\n")
        rc, out, err, seen = self.run_config([], env)
        self.assertEqual((rc, seen), (0, []))                       # local: it talks to no hub
        self.assertIn("https://from.file", out)
        self.assertIn(str(path), out)
        self.assertIn("set", out)
        self.assertNotIn("s3cret-value", out + err)                 # the token is never printed

    def test_config_says_unset_when_there_is_no_token(self):
        rc, out, _, _ = self.run_config([], {"HOME": tempfile.mkdtemp()})
        self.assertEqual(rc, 0)
        self.assertIn("unset", out)
        self.assertIn("http://127.0.0.1:8000", out)

    def test_save_writes_0600_in_a_0700_directory(self):
        home = tempfile.mkdtemp()
        env = {"HOME": home}
        rc, out, _, _ = self.run_config(["--save"], env, hub=("--hub", "https://saved.example", "--token", "s3cret-value"))
        p = pathlib.Path(home) / ".config/sweep/env"
        self.assertEqual((rc, p.is_file()), (0, True))
        self.assertEqual((mode_of(p), mode_of(p.parent)), (0o600, 0o700))
        self.assertNotIn("s3cret-value", out)
        self.assertEqual(cliconfig.read(env), {"SWEEP_HUB": "https://saved.example", "SWEEP_TOKEN": "s3cret-value"})

    def test_save_keeps_a_value_that_resolved_from_the_file(self):
        path, env = config_file("SWEEP_HUB=https://old.example\nSWEEP_TOKEN=kept-token\n")
        self.run_config(["--save"], env, hub=("--hub", "https://new.example"))
        self.assertEqual(cliconfig.read(env), {"SWEEP_HUB": "https://new.example", "SWEEP_TOKEN": "kept-token"})

    def test_a_loose_file_refuses_even_on_save_and_chmod_is_the_whole_fix(self):
        # --save cannot quietly tighten a loose file: it would have to READ it first to keep the values already
        # there, and a file others can read is exactly what must not be read. chmod, then save.
        path, env = config_file("SWEEP_HUB=https://hub.example\n", mode=0o644)
        with self.assertRaises(SystemExit) as cm:
            self.run_config(["--save"], env, hub=("--hub", "https://hub.example", "--token", "t"))
        self.assertIn(f"chmod 600 {path}", str(cm.exception))
        path.chmod(0o600)
        self.run_config(["--save"], env, hub=("--hub", "https://hub.example", "--token", "t"))
        self.assertEqual(mode_of(path), 0o600)

    def test_save_with_nothing_to_write_is_refused(self):
        with self.assertRaises(SystemExit) as cm:
            self.run_config(["--save"], {"HOME": tempfile.mkdtemp()})
        self.assertRegex(str(cm.exception), r"^REFUSING: .+ -- .+$")
        self.assertIn("--token", str(cm.exception))


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


class PathsAndStreams(unittest.TestCase):
    def test_path_parameters_are_flags_that_fill_the_path(self):
        _, _, _, seen = run(["abandon", "--run-id", "encode-x-1", "--reason", "wrong settings"], lambda r: httpx.Response(200, json={"ok": True}))
        self.assertEqual((seen[0].method, str(seen[0].url)), ("POST", "http://hub.test/runs/encode-x-1/abandon"))
        self.assertEqual(json.loads(seen[0].content), {"reason": "wrong settings"})

    def test_watch_prints_the_stream_and_exits_by_the_final_state(self):
        sse = 'data: {"state": "running"}\n\ndata: {"state": "failed", "detail": "heartbeat expired", "final": true}\n\n'
        rc, out, _, seen = run(["watch", "--run-id", "encode-x-1"], lambda r: httpx.Response(200, text=sse, headers={"content-type": "text/event-stream"}))
        self.assertEqual((rc, str(seen[0].url)), (1, "http://hub.test/runs/encode-x-1/watch"))
        self.assertEqual(out.splitlines(), ['{"state": "running"}', '{"state": "failed", "detail": "heartbeat expired", "final": true}'])
        ok = 'data: {"state": "complete", "final": true}\n\n'
        rc, _, _, _ = run(["watch", "--run-id", "encode-x-1"], lambda r: httpx.Response(200, text=ok, headers={"content-type": "text/event-stream"}))
        self.assertEqual(rc, 0)


class VerbsMatchTheApp(unittest.TestCase):
    def test_every_verb_has_a_subparser_and_the_flags_are_the_fields(self):
        store = Store()
        self.addCleanup(store.close)
        spec = create_app(store, None).openapi()
        expected = {}
        for path, ops in spec["paths"].items():
            for method, op in ops.items():
                if "agents" in op.get("tags", []):
                    continue                                   # the agent is their client; they have no CLI verb
                fields = []
                body = op.get("requestBody")
                if body:
                    ref = body["content"]["application/json"]["schema"]["$ref"].rsplit("/", 1)[-1]
                    fields = list(spec["components"]["schemas"][ref]["properties"])
                expected[(method.upper(), path)] = fields
        by_route = {(v.method, v.path): name for name, v in cli.VERBS.items()}
        self.assertEqual(set(by_route), set(expected), "the CLI's verbs are the app's, no more and no fewer")
        for name, verb in cli.VERBS.items():
            with self.subTest(verb=name):
                self.assertEqual([f for f, _ in verb.flags], expected[(verb.method, verb.path)])
                self.assertEqual(list(verb.params), [p[1:-1] for p in verb.path.split("/") if p.startswith("{")])

    def test_the_guide_names_every_verb_and_invokes_none_that_is_gone(self):
        """docs/GUIDE.md is the how-to, and this keeps its verb LIST from falling behind the CLI in either direction:
        a new endpoint cannot land without a line there, and a renamed one cannot leave a stale invocation behind.

        It holds the list and nothing else. The guide's prose can still go stale and the guide says so itself.

        Coverage counts any mention -- in a command or in backticks. The other direction counts only INVOCATIONS a
        reader could copy and run: `sweep <verb>` at the start of a line, after a `$ ` prompt, or opening a backticked
        span, with any global options between. Mid-sentence prose is not an invocation ("a ladder sweep across arms"
        is English, not a command), and backticked words alone are not either -- `host`, `check` and `time` are
        ordinary words. `sweep-node` has its own subcommands and is excluded by the space required after `sweep`.
        """
        text = (pathlib.Path(__file__).resolve().parent.parent / "docs" / "GUIDE.md").read_text()
        invoked = set(re.findall(r"(?:^|\$ |`)sweep(?:\s+--[a-z-]+\s+\S+)*\s+([a-z][a-z-]*)", text, re.M))
        mentioned = invoked | set(re.findall(r"`([a-z][a-z-]*)`", text))
        known = set(cli.VERBS) | set(cli.LOCAL)          # LOCAL reaches no endpoint but still has to be documented
        missing = known - mentioned
        gone = invoked - known
        self.assertFalse(missing, f"docs/GUIDE.md names no {sorted(missing)}")
        self.assertFalse(gone, f"docs/GUIDE.md invokes {sorted(gone)}, which the CLI does not have")

    def test_help_lists_every_verb(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit):
            cli.main(["--help"])
        for name in cli.VERBS:
            self.assertIn(name, out.getvalue())


if __name__ == "__main__":
    unittest.main()
