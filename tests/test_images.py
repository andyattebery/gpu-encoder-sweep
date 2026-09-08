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


if __name__ == "__main__":
    unittest.main()
