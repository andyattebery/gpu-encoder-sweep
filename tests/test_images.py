#!/usr/bin/env python3
"""The package names its commit, and the images pin every base and install from the lock.

    python3 -m unittest tests.test_images
"""
import importlib.metadata
import pathlib
import re
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent


class Version(unittest.TestCase):
    def test_version_is_pep440_from_git(self):
        # X.Y.Z at a tag (CI runs on tag pushes too), X.Y.Z.devN+g<sha> past one, .d<date> on a dirty tree
        version = importlib.metadata.version("gpu-encoder-sweep")
        self.assertRegex(version, r"^\d+(\.\d+)*(\.dev\d+)?(\+g[0-9a-f]{7,}(\.d\d{8})?)?$")


class Dockerfiles(unittest.TestCase):
    FILES = ("hub", "node-encode", "node-encode-mesarc", "node-score")

    def test_every_base_is_pinned_and_the_package_comes_from_the_lock(self):
        for name in self.FILES:
            text = (ROOT / "docker" / f"Dockerfile.{name}").read_text()
            with self.subTest(image=name):
                froms = [[t for t in l.split()[1:] if not t.startswith("--")][0] for l in text.splitlines() if l.startswith("FROM ")]
                self.assertTrue(froms)
                for ref in froms:
                    self.assertNotIn(":latest", ref, ref)
                    self.assertTrue("@sha256:" in ref or ":" in ref.rsplit("/", 1)[-1], f"{ref} carries no tag or digest")
                self.assertIn("uv sync --locked", text)
                self.assertIn("/etc/sweep-artifact", text)
                self.assertNotIn(":latest", text.replace("tdarr_node:latest resolved", ""))    # the comment may say where the digest came from

    def test_the_images_workflow_builds_every_image_and_pins_its_actions(self):
        text = (ROOT / ".github" / "workflows" / "images.yaml").read_text()
        for name in self.FILES:
            self.assertIn(name, text)
        self.assertRegex(text, r"uses: docker/build-push-action@v\d+")
        self.assertIn("SETUPTOOLS_SCM_PRETEND_VERSION=", text)
        self.assertIn("packages: write", text)


class Workflows(unittest.TestCase):
    WORKFLOWS = ("ci", "images")

    def test_setup_uv_is_pinned_by_commit_because_it_publishes_no_major_tag(self):
        # astral-sh/setup-uv stopped moving a vN tag after v7; `@v10` resolved to nothing and both workflows died at
        # "Set up job" on the first push of M2 (2026-09-08). Its README pins the commit with the version beside it.
        for name in self.WORKFLOWS:
            text = (ROOT / ".github" / "workflows" / f"{name}.yaml").read_text()
            uses = [l.strip() for l in text.splitlines() if "astral-sh/setup-uv" in l]
            with self.subTest(workflow=name):
                self.assertTrue(uses, f"{name}.yaml does not set up uv")
                for line in uses:
                    self.assertRegex(line, r"^- uses: astral-sh/setup-uv@[0-9a-f]{40} # v\d+\.\d+\.\d+$", line)


if __name__ == "__main__":
    unittest.main()
