#!/usr/bin/env python3
"""sweep/recipes.py: the pure functions named by recipe -- K1 (the cell key), the nearest-rank pooling of S1, G1's geometry.

    python3 -m unittest tests.test_recipes
"""
import unittest

from sweep import recipes

B580 = "intel-b580-ihd26.2.2-qsv-av1"
SETTINGS = [("qsv.q", "24"), ("qsv.preset", "4"), ("qsv.b_strategy", "0")]


class CellKey(unittest.TestCase):
    def test_golden(self):
        # sha256 of the canonical JSON (keys sorted, separators , and :) of unit, cut sha, cut kind, window, the identity
        # settings as a sorted list of [id, value], the ffmpeg version string; the first 32 hex characters (SPEC K1)
        self.assertEqual(recipes.cell_key(B580, "sha-tng-ref", "reference", "tng", SETTINGS, "8.1.2-Jellyfin"),
                         "743f6054c08c68eb9acdfeed23e2b232")

    def test_order_independent(self):
        a = recipes.cell_key(B580, "sha-tng-ref", "reference", "tng", SETTINGS, "8.1.2-Jellyfin")
        b = recipes.cell_key(B580, "sha-tng-ref", "reference", "tng", list(reversed(SETTINGS)), "8.1.2-Jellyfin")
        self.assertEqual(a, b)
        self.assertEqual(len(a), 32)

    def test_changes_with_the_version_the_cut_and_the_kind(self):
        base = recipes.cell_key(B580, "sha-tng-ref", "reference", "tng", SETTINGS, "8.1.2-Jellyfin")
        self.assertNotEqual(base, recipes.cell_key(B580, "sha-tng-ref", "reference", "tng", SETTINGS, "8.1.3-Jellyfin"))
        self.assertNotEqual(base, recipes.cell_key(B580, "sha-tng-src", "source", "tng", SETTINGS, "8.1.2-Jellyfin"))
        self.assertNotEqual(base, recipes.cell_key(B580, "sha-tng-ref", "reference", "tng", SETTINGS + [("qsv.adaptive_b", "-1")], "8.1.2-Jellyfin"))

    def test_takes_identity_pairs_only(self):
        # the caller filters computed and default_resolved settings out; a triple is refused rather than silently hashed
        with self.assertRaises(ValueError):
            recipes.cell_key(B580, "sha-tng-ref", "reference", "tng", [("qsv.q", "24", "identity")], "8.1.2-Jellyfin")


class NearestRank(unittest.TestCase):
    VALUES = [100, 10, 90, 20, 80, 30, 70, 40, 60, 50]      # ten values, unsorted on purpose

    def test_ceils(self):
        # ceil(p * n) - 1 into the ascending list: p = 0.25 on ten values is index 2, the third smallest; the retired fork's
        # floor read index 1 and was one frame off for every cell
        self.assertEqual(recipes.nearest_rank(self.VALUES, 0.25), 30)
        self.assertEqual(recipes.nearest_rank(self.VALUES, 0.5), 50)
        self.assertEqual(recipes.nearest_rank(self.VALUES, 1.0), 100)
        self.assertEqual(recipes.nearest_rank(self.VALUES, 0.05), 10)

    def test_pool_statistics(self):
        self.assertEqual(recipes.pool(self.VALUES), {"mean": 55.0, "p5": 10, "min": 10, "max": 100})
        with self.assertRaises(ValueError):
            recipes.pool([])


class Geometry(unittest.TestCase):
    def test_even_and_height_limited(self):
        # G1: 16:9 on the M4 panel is 2752x1548; a 2.4:1 source scales to the same height, both dimensions forced even
        self.assertEqual(recipes.geometry(1920, 1080, 1548), (2752, 1548))
        self.assertEqual(recipes.geometry(1920, 800, 1548), (3714, 1548))   # 3715.2 rounds to 3715; & ~1 makes it 3714, as the archive did
        self.assertEqual(recipes.geometry(1920, 1080, 1080), (1920, 1080))
        self.assertEqual(recipes.geometry(1920, 1080, 1251), (2224, 1250))


if __name__ == "__main__":
    unittest.main()
