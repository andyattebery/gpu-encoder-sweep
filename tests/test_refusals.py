#!/usr/bin/env python3
"""The refusal inventory against the code: every by_construction refusal is in ARCHITECTURE's table, every check
refusal names a view or script check that exists, no free-input body outside the catalogue carries a reserved
field name, and every refusal text in the code base takes the one form -- composed in one place.

    python3 -m unittest tests.test_refusals
"""
import ast
import json
import pathlib
import re
import unittest

from sweep import model_check as mc
from sweep.hub import refusals
from sweep.hub.app import create_app
from sweep.hub.queue import FakeQueue
from sweep.hub.store import Store

ROOT = pathlib.Path(__file__).resolve().parent.parent
FORM = r"^REFUSING: .+ -- .+$"


def entries():
    data = json.loads((ROOT / "docs" / "refusals.json").read_text())
    return [e for section, items in data.items() if section != "_comment" for e in items]


class Inventory(unittest.TestCase):
    def test_every_by_construction_id_is_in_architectures_table(self):
        ids = {e["id"] for e in entries() if e["disposition"] == "by_construction"}
        text = (ROOT / "docs" / "ARCHITECTURE.md").read_text()
        start = text.index("### How the `by_construction` class stays closed")
        end = text.index("\n### ", start + 1)
        cells = set(re.findall(r"^\| `([^`]+)` \|", text[start:end], re.M))
        self.assertEqual(cells, ids)

    def test_every_check_id_maps_to_a_view_or_script_check(self):
        conn = mc.load_schema()
        self.addCleanup(conn.close)
        views = set(mc.db_views(conn, "x_"))
        for e in [e for e in entries() if e["disposition"] == "check"]:
            with self.subTest(id=e["id"]):
                named = set(re.findall(r"`(x_\w+)`", e["how"])) | {k for k in mc.SCRIPT_CHECKS if f"`{k}`" in e["how"]}
                self.assertTrue(named, f"{e['id']}: how names no check: {e['how']}")
                for name in named:
                    if name.startswith("x_"):
                        self.assertIn(name, views, e["id"])


class FieldNames(unittest.TestCase):
    RESERVED = {"count", "device", "height", "directory", "root"}

    def properties(self, schema, schemas, seen=()):
        """Every property name reachable from a body schema, through $ref, arrays and unions."""
        if "$ref" in schema:
            name = schema["$ref"].rsplit("/", 1)[-1]
            if name in seen:
                return set()
            return self.properties(schemas[name], schemas, seen + (name,))
        names = set(schema.get("properties", {}))
        for prop in schema.get("properties", {}).values():
            names |= self.properties(prop, schemas, seen)
        if "items" in schema:
            names |= self.properties(schema["items"], schemas, seen)
        for key in ("anyOf", "oneOf", "allOf"):
            for sub in schema.get(key, []):
                names |= self.properties(sub, schemas, seen)
        return names

    def test_no_free_count_device_height_directory_root_outside_catalogue(self):
        spec = create_app(Store(), FakeQueue()).openapi()
        schemas = spec.get("components", {}).get("schemas", {})
        checked = 0
        for path, ops in spec["paths"].items():
            for op in ops.values():
                if "catalogue" in op.get("tags", []) or "requestBody" not in op:
                    continue
                names = self.properties(op["requestBody"]["content"]["application/json"]["schema"], schemas)
                self.assertFalse(names & self.RESERVED, f"{path}: {names & self.RESERVED}")
                checked += 1
        self.assertGreater(checked, 0)


class Form(unittest.TestCase):
    def test_every_fix_is_in_the_refusing_form(self):
        _, checks = mc.parse_tags()
        for view, c in checks.items():
            self.assertRegex(mc.refusing(c["check"], c["fix"]), FORM, view)
        for name, (refuses, fix) in mc.SCRIPT_CHECKS.items():
            self.assertRegex(mc.refusing(refuses, fix), FORM, name)
        for name, (what, fix) in refusals.CONSTRAINT_FIXES.items():
            self.assertRegex(mc.refusing(what, fix), FORM, name)
        for fix in list(refusals.NOT_NULL_FIXES.values()) + list(refusals.UNIQUE_FIXES.values()) + [refusals.UNIQUE_DEFAULT]:
            self.assertRegex(mc.refusing("a thing", fix), FORM, fix)

    def test_no_refusal_text_is_composed_outside_refusing(self):
        # the one place `REFUSING:` is spelled in code is model_check.refusing; every other refusal goes through it
        found = []
        for py in sorted((ROOT / "sweep").rglob("*.py")):
            tree = ast.parse(py.read_text())
            docstrings = set()
            for node in ast.walk(tree):
                if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and node.body:
                    first = node.body[0]
                    if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
                        docstrings.add(id(first.value))
            for node in ast.walk(tree):
                if isinstance(node, ast.Constant) and isinstance(node.value, str) and "REFUSING:" in node.value and id(node) not in docstrings:
                    found.append((str(py.relative_to(ROOT)), node.value))
        self.assertEqual(found, [("sweep/model_check.py", "REFUSING: ")])


if __name__ == "__main__":
    unittest.main()
