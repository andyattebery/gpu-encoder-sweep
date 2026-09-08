"""sweep/recipes.py -- the pure functions named by recipe (docs/SPEC.md, "The recipes"): K1 the cell key, S1's pooling,
G1's geometry. Shared by the hub and the node; stdlib only; nothing here reads the store or runs a tool. A changed recipe
is a new name and a re-run, never a silent drift, which is why these are small and named.
"""
import hashlib
import json
import math


def cell_key(encoder_unit_id, content_sha, cut_kind, window_id, identity_settings, ffmpeg_version):
    """K1: sha256 of the canonical JSON (keys sorted, separators `,` and `:`) of the unit, the cut's content sha, the cut
    kind, the window, the identity settings as a sorted list of [setting_id, value], and the ffmpeg version string; the
    first 32 hex characters. Excluded on purpose: the ffmpeg build sha and the driver runtime (recorded on the run), the
    host (not a factor), and every `computed` or `default_resolved` setting -- so the caller passes identity pairs only,
    and a triple is refused rather than hashed. These keys do not equal the archived harness's (sweep.py:1398).
    """
    pairs = []
    for item in identity_settings:
        if len(item) != 2:
            raise ValueError(f"cell_key takes (setting_id, value) pairs, the identity settings only; got {item!r}")
        pairs.append([str(item[0]), str(item[1])])
    payload = {"encoder_unit_id": encoder_unit_id, "content_sha": content_sha, "cut_kind": cut_kind, "window_id": window_id,
               "settings": sorted(pairs), "ffmpeg_version": ffmpeg_version}
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:32]


def nearest_rank(values, p):
    """S1 step 2: the p-quantile by nearest rank -- `ceil(p * n) - 1` into the ascending list. Never floor, never
    `round(p * (n - 1))`: the retired fork floored it and read one frame off for every cell (sweep.py:1065)."""
    if not values:
        raise ValueError("nearest_rank of no values")
    if not 0 < p <= 1:
        raise ValueError(f"p must be in (0, 1], got {p!r}")
    ordered = sorted(values)
    return ordered[max(0, math.ceil(p * len(ordered)) - 1)]


def pool(values):
    """S1 step 2: the statistics stored per metric -- mean, p5, min, max of the per-frame values. p95 is computed by a
    reader who wants it and is not stored; `score.statistic` holds these four and nothing else."""
    if not values:
        raise ValueError("pool of no values")
    ordered = sorted(values)
    return {"mean": sum(ordered) / len(ordered), "p5": nearest_rank(ordered, 0.05), "min": ordered[0], "max": ordered[-1]}


def geometry(native_w, native_h, height):
    """G1, the height-limited case: scale a native_w x native_h source to `height` keeping its aspect, both dimensions
    forced even (yuv420 needs them so; the archive's `& ~1`, sweep.py:1866). 16:9 at 1548 is 2752 x 1548; the 10.5" panel's
    raw 1251 becomes 1250. The width-limited case (a source wider than the panel's aspect) is not modelled yet."""
    w = round(native_w * height / native_h)
    return (w & ~1, height & ~1)
