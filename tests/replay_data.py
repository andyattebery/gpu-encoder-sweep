"""The proof's fixture (sweep/model_check.FIXTURE) as the verb bodies, run plans and records that build it, in the
order the campaign would: what tests/test_hub_replay.py drives through the API. Not a test module.

Every value here mirrors the fixture's SQL; the equality test on the authored tables is what keeps the two agreeing.
Cells that the fixture generates with SELECTs are generated here with the same comprehensions.
"""
from sweep.hub.store import CellPlan, RunPlan

B580, A4000, TI5060 = "intel-b580-ihd26.2.2-qsv-av1", "nvidia-a4000-595-nvenc-hevc", "nvidia-5060ti-595-nvenc-av1"
CLASS, SEARCH, LANE = "native-1080p-sdr", "b580-qsv-av1", "m4-ipad-le1080p-sdr"
MEMBERS = ["mrrobot", "parks", "shield", "snowpiercer", "tng", "tos"]
WINDOWS = MEMBERS + ["sopranos"]
NODE = dict(artifact="sweep-node@sha256:0a1b2c", ffmpeg_build="8.1.2-Jellyfin", ffmpeg_sha="0b0ea2d", harness_version="g0")
SCORER = dict(artifact="sweep-score@sha256:9f8e7d", ffmpeg_build="8.1.2-Jellyfin", ffmpeg_sha="0b0ea2d", harness_version="g0",
              scorer_build="FFVship 1.3 + 8.1.2-0b0ea2d", ffvship_version="1.3", metric_backend="libvmaf_cuda")
HEVC_RUNGS = [4, 6, 8, 10, 11, 14, 15, 16, 17, 18, 20, 22, 26, 28, 30, 32, 34, 36, 38, 42, 46]
AV1_RUNGS = [15, 20, 22, 24, 25, 26, 28, 30, 34, 35, 40, 45, 50, 55, 60]
IN_RANGE = [r for r in AV1_RUNGS if 1 <= r <= 51]        # qsv.q ends at 51: 55 and 60 are UNREACHABLE


def verb(path, body):
    return ("verb", path, body)


def run(plan, started_at, finished_at, records=()):
    """A run's whole life in one transaction: planned, launched (hub), running (agent), its records, complete (hub)."""
    return ("run", plan, started_at, finished_at, list(records))


def calibrate(name, run_id, value, computed_at):
    return ("calibrate", name, run_id, value, computed_at)


def plan(run_id, stage, host, node_label, windows=(), cells=(), unit=B580, cc=CLASS, search=SEARCH, started_at=None, **more):
    return RunPlan(run_id=run_id, stage=stage, host=host, node_label=node_label, encoder_unit_id=unit, content_class_id=cc,
                   search_id=search, windows=tuple(windows), cells=tuple(cells), planned_at=started_at, **more)


def cell(key, window, cut_kind, settings):
    return CellPlan(key, window, cut_kind, tuple(settings))


def qsv(q, preset, bs, adaptive_b=True):
    s = [("qsv.q", str(q), "identity"), ("qsv.preset", preset, "identity"), ("qsv.b_strategy", bs, "identity")]
    return s + ([("qsv.adaptive_b", "-1", "default_resolved")] if adaptive_b else [])


def encode(key, bytes_, bitrate, kept=False, decode_path="hardware"):
    return {"kind": "encode", "cell_key": key, "bytes": bytes_, "bitrate_kbps": bitrate, "frames": 1439, "duration_s": 60.0,
            "decode_path": decode_path, "kept": kept}


def score(key, q, steps=()):
    return {"kind": "score", "cell_key": key, "recipe": "S1", "kept": False,
            "scores": [{"metric": "ssimulacra2", "statistic": "mean", "value": 95.0 - 0.5 * q},
                       {"metric": "ssimulacra2", "statistic": "p5", "value": 86.0 - 0.5 * q},
                       {"metric": "ssimulacra2", "statistic": "min", "value": 80.0 - 0.5 * q},
                       {"metric": "vmaf", "statistic": "mean", "value": 96.0},
                       {"metric": "cambi", "statistic": "mean", "value": 0.4},
                       {"metric": "butteraugli", "statistic": "max", "value": 3.1}],
            "steps": list(steps)}


# ---------------------------------------------------------------- the catalogue

HOSTS = [
    dict(host="nas-01", machine="nas-01", ssh_host="nas-01", os="linux", work_root="/srv/sweep", share_root="/srv/sweep", notes="the hub; the share is local"),
    dict(host="media-01", machine="media-01", ssh_host="media-01", os="linux", work_root="/mnt/data/sweep", share_root="/mnt/nas-01/sweep", ffmpeg="/opt/jellyfin-ffmpeg/bin/ffmpeg"),
    dict(host="media-01-score", machine="media-01", ssh_host="media-01", os="linux", work_root="/mnt/data/sweep-score", share_root="/mnt/nas-01/sweep",
         local_view="/mnt/data/sweep", notes="the score container; sees the encode container's work root at the same path"),
    dict(host="htpc-01", machine="htpc-01", ssh_host="htpc-01", os="linux", work_root="/run/media/system/data/sweep", share_root="/mnt/nas-01/sweep",
         ffmpeg="/ffmpeg/ffmpeg", notes="root podman; the bind mount is the patched build"),
    dict(host="eta", machine="eta", ssh_host="eta", os="windows", work_root="D:\\sweep", share_root="\\\\nas-01\\sweep",
         ffmpeg="c:\\Program Files\\jellyfin-ffmpeg\\bin\\ffmpeg.exe", notes="native Windows, no bash"),
    dict(host="eta-wsl", machine="eta", ssh_host="eta", os="linux", work_root="/home/sweep/work", share_root="/mnt/nas-01/sweep", local_view="/mnt/d",
         notes="the score container under WSL; eta's own cells are scored through /mnt/d"),
]
UNITS = [
    dict(encoder_unit_id=B580, vendor="intel", card="Arc B580", driver="iHD 26.2.2", frontend="qsv", codec="av1", host="media-01", device="/dev/dri/by-path/pci-0000:03:00.0-render"),
    dict(encoder_unit_id=A4000, vendor="nvidia", card="RTX A4000", driver="595.71.05", frontend="nvenc", codec="hevc", host="media-01", device="pci-0000:41:00.0"),
    dict(encoder_unit_id=TI5060, vendor="nvidia", card="RTX 5060 Ti", driver="595.71.05", frontend="nvenc", codec="av1", host="eta", device="pci-0000:01:00.0"),
]
CONCEPTS = [
    ("quality_anchor", "a position on the codec ladder: -qp, -cq, -global_quality, -q:v"),
    ("rate_control_mode", "constant-quantiser versus rate-targeted: -rc constqp, -rc_mode CQP, qsv -q:v"),
    ("preset", "the speed/quality ordinal: nvenc p1..p7, qsv 1..7, vaapi -compression_level"),
    ("b_pyramid", "B-frame reference structure"),
    ("tiling", "AV1 tile columns and rows"),
]


def setting(setting_id, flag, frontend, kind, subsystem, value_type, range_lo=None, range_hi=None, is_generic=False, notes=None,
            enum_values=(), roles=(), scope=()):
    return dict(setting_id=setting_id, flag=flag, frontend=frontend, kind=kind, subsystem=subsystem, value_type=value_type, range_lo=range_lo,
                range_hi=range_hi, is_generic=is_generic, notes=notes, enum_values=list(enum_values), roles=list(roles), scope=list(scope))


def scope(default_value, default_is_measured):
    return dict(encoder_unit_id=B580, applies=True, default_value=default_value, default_is_measured=default_is_measured)


SETTINGS = [
    setting("qsv.preset", "-preset", "qsv", "ordinal", "other", "enum", notes="1 is BEST_QUALITY, 7 is fastest", enum_values=[str(i) for i in range(1, 8)],
            roles=["preset"], scope=[scope("4", False)]),
    setting("qsv.q", "-q:v", "qsv", "quality_anchor", "rate_control", "int", 1, 51, notes="selects CQP AND is the anchor", roles=["quality_anchor", "rate_control_mode"]),
    setting("qsv.b_strategy", "-b_strategy", "qsv", "option", "frame_types", "enum", notes="default -1 is byte-identical to 1", enum_values=["-1", "0", "1"],
            roles=["b_pyramid"], scope=[scope("-1", True)]),
    setting("qsv.adaptive_b", "-adaptive_b", "qsv", "option", "frame_types", "enum", enum_values=["-1", "0", "1"], scope=[scope("-1", False)]),
    setting("qsv.tile_cols", "-tile_cols", "qsv", "option", "tiles", "int", 1, 64, roles=["tiling"]),
    setting("qsv.tile_rows", "-tile_rows", "qsv", "option", "tiles", "int", 1, 64, roles=["tiling"]),
    setting("qsv.b_v", "-b:v", "qsv", "option", "rate_control", "int", 0, None, notes="a rate request; computed from the ceiling"),
    setting("nvenc.qp", "-qp", "nvenc", "quality_anchor", "rate_control", "int", 0, 51, roles=["quality_anchor"]),
    setting("nvenc.cq", "-cq", "nvenc", "quality_anchor", "rate_control", "int", 0, 51, roles=["quality_anchor"]),
    setting("nvenc.b_v", "-b:v", "nvenc", "option", "rate_control", "int", 0, None, notes="a rate request; computed from the ceiling"),
    setting("nvenc.rc", "-rc", "nvenc", "mode_selector", "rate_control", "enum", enum_values=["constqp", "vbr", "cbr"], roles=["rate_control_mode"]),
    setting("nvenc.preset", "-preset", "nvenc", "ordinal", "other", "enum", enum_values=[f"p{i}" for i in range(1, 8)], roles=["preset"]),
    setting("nvenc.tune", "-tune", "nvenc", "option", "other", "enum", notes="uhq needs the nv-codec-headers pin", enum_values=["hq", "uhq", "ll"]),
    setting("generic.compression_level", "-compression_level", None, "option", "plumbing", "int", 0, 12, is_generic=True,
            notes="absent from every private dump; a size lever on AMD, a speed lever on the B580", roles=["preset"], scope=[scope("0", False)]),
]
CONSTANTS = [
    dict(name="CEILING", value=22.0, unit="Mbps muxed", provenance="derived", inputs_json='{"device_bytes":512e9,"hours":50}', precision="±~10%", cites_json='["RESULTS §16"]'),
    dict(name="MARGIN", value=0.20, unit="fraction", provenance="policy", reason="the remux skip threshold; bounded below by the probe precision (median SE 14.1%), RESULTS §19.8", cites_json='["RESULTS §19.8"]'),
    dict(name="HEADROOM", unit="fraction of CEILING", provenance="measured", cites_json='["RESULTS §18.6b"]'),
    dict(name="BOUND", unit="qp", provenance="measured", cites_json='["RESULTS §18.6a"]'),
    dict(name="RUNG_FACTOR", unit="x on bitrate per rung", provenance="measured", cites_json='["RESULTS §18.6a"]'),
    dict(name="HOST_THRESHOLD", unit="Mbps", provenance="measured", cites_json='["RESULTS §18.5a"]'),
]
AUDIO, SUBS = "all default-flagged, else stream 0", "text -> mov_text"
KIDS = dict(codec="hevc", decision_rule="incumbent", audio=AUDIO, subtitles=SUBS, score_height=1250, bitrate_cap_binds="never", steps=["quality-target-encode"])
M4 = dict(audio=AUDIO, subtitles=SUBS, score_height=1548, bitrate_cap_constant="CEILING", decision_rule="cap",
          steps=["probe", "remux", "quality-target-encode", "bitrate-target-encode"])
LANES = [
    dict(KIDS, lane="kids-ipad-standard-sdr", input_dynamic_range="sdr", output_resolution="1080p", output_dynamic_range="sdr", hdr_handling="n/a", has_content=True),
    dict(KIDS, lane="kids-ipad-standard-hdr", input_dynamic_range="hdr", output_resolution="1080p", output_dynamic_range="sdr", hdr_handling="tonemapping", has_content=True),
    dict(KIDS, lane="kids-ipad-2d-animation-sdr", input_dynamic_range="sdr", output_resolution="native", output_dynamic_range="sdr", hdr_handling="n/a", has_content=True),
    dict(KIDS, lane="kids-ipad-2d-animation-hdr", input_dynamic_range="hdr", output_resolution="native", output_dynamic_range="sdr", hdr_handling="tonemapping", has_content=False),
    dict(M4, lane=LANE, codec="av1", input_width_max=1920, input_dynamic_range="sdr", output_resolution="native", output_dynamic_range="sdr", hdr_handling="n/a",
         bitrate_cap_binds="rarely", has_content=True),
    dict(M4, lane="m4-ipad-le1080p-hdr", codec="hevc", input_width_max=1920, input_dynamic_range="hdr", output_resolution="native", output_dynamic_range="hdr",
         hdr_handling="passthrough", bitrate_cap_binds="rarely", has_content=False),
    dict(M4, lane="m4-ipad-gt1080p-sdr", codec="hevc", input_width_min=1921, input_dynamic_range="sdr", output_resolution="native", output_dynamic_range="sdr",
         hdr_handling="n/a", bitrate_cap_binds="always", has_content=True),
    dict(M4, lane="m4-ipad-gt1080p-hdr", codec="hevc", input_width_min=1921, input_dynamic_range="hdr", output_resolution="native", output_dynamic_range="hdr",
         hdr_handling="passthrough", bitrate_cap_binds="always", has_content=True),
]
M4_LANES = [LANE, "m4-ipad-le1080p-hdr", "m4-ipad-gt1080p-sdr", "m4-ipad-gt1080p-hdr"]
SCOPES = [("CEILING", M4_LANES), ("MARGIN", M4_LANES), ("BOUND", M4_LANES[1:]), ("RUNG_FACTOR", M4_LANES[1:]), ("HOST_THRESHOLD", M4_LANES[2:]),
          ("HEADROOM", M4_LANES)]
TITLE = dict(kind="title", library="TV Shows", width=1920, height=1080, dynamic_range="sdr", video_codec="h264", field_order="progressive", fps=23.976,
             bit_depth=8, scanned_at="2026-08-24")
TITLES = [
    dict(TITLE, title_id="mrrobot", path="/mnt/storage/TV Shows/Mr. Robot/S01E01.mkv", bitrate_kbps=29200, bpp=0.587, source_type="remux"),
    dict(TITLE, title_id="parks", path="/mnt/storage/TV Shows/Parks and Recreation/S03E01.mkv", bitrate_kbps=12200, bpp=0.245, source_type="web"),
    dict(TITLE, title_id="shield", path="/mnt/storage/TV Shows/Agents of SHIELD/S01E01.mkv", bitrate_kbps=27400, bpp=0.551, source_type="remux"),
    dict(TITLE, title_id="snowpiercer", path="/mnt/storage/Movies/Snowpiercer (2013)/Snowpiercer.mkv", library="Movies", bitrate_kbps=39000, bpp=0.784, source_type="remux"),
    dict(TITLE, title_id="sopranos", path="/mnt/storage/TV Shows/The Sopranos/S01E01.mkv", bitrate_kbps=29800, bpp=0.599, source_type="remux"),
    dict(TITLE, title_id="tng", path="/mnt/storage/TV Shows/Star Trek TNG/S05E01.mkv", bitrate_kbps=23200, bpp=0.467, source_type="remux"),
    dict(TITLE, title_id="tos", path="/mnt/storage/TV Shows/Star Trek TOS/S02E01.mkv", video_codec="vc1", bitrate_kbps=18800, bpp=0.378, source_type="bluray"),
    dict(TITLE, title_id="interstellar", path="/mnt/storage/Movies/Interstellar (2014)/Interstellar.mkv", library="Movies", width=3840, height=2160,
         dynamic_range="hdr10", video_codec="hevc", bit_depth=10, bitrate_kbps=50000, bpp=0.251, source_type="remux"),
    dict(TITLE, title_id="fixture-4k-sdr", path="/mnt/storage/Movies/Fixture (2020)/Fixture.mkv", library="Movies", width=3840, height=2160,
         video_codec="hevc", bit_depth=10, bitrate_kbps=40000, bpp=0.201, source_type="remux"),
]
PINS = [
    ("mrrobot", 1201.0, "dark and noisy", 88.2, "darkest and noisiest in the library"),
    ("parks", 402.5, "well-lit grain-free", 73.5, "flat fluorescent light; keeps the set off all-REMUX"),
    ("shield", 1510.0, "sustained motion", 80.1, "modern VFX and sustained motion"),
    ("snowpiercer", 3300.0, "sustained darkness", 61.0, "hard-content probe"),
    ("sopranos", 900.0, "film grain", 70.4, "dropped from the 1080p class: drama shot on film is not what the lane carries"),
    ("tng", 1400.0, "film grain", 66.7, "1980s film scan, heavy grain"),
    ("tos", 777.0, "film grain", 64.0, "the only non-h264 in the set; no VC-1 decoder on the B580"),
]
CHAINS = [
    (LANE, "media-01", B580, "null"), (LANE, "eta", TI5060, "null"), ("m4-ipad-gt1080p-sdr", "media-01", A4000, "null"),
    ("kids-ipad-2d-animation-hdr", "media-01", A4000, "hwupload_cuda,tonemap_cuda=...,scale_cuda=..."),
]
STRATA = [
    dict(stratum="remux", kind="inventory", definition="t.source_type = 'remux'"),
    dict(stratum="web", kind="inventory", definition="t.source_type = 'web'"),
    dict(stratum="non-h264 decode", kind="inventory", definition="t.video_codec <> 'h264'"),
    dict(stratum="bpp p90", kind="quantile", definition="t.bpp >= 0.55"),
    dict(stratum="dark and noisy", kind="character", definition="dark and noisy", share_estimate=0.40),
    dict(stratum="well-lit grain-free", kind="character", definition="well-lit grain-free", share_estimate=0.20),
]

# ---------------------------------------------------------------- the runs' cells and records

SCREEN_CELLS = {
    "s-bs0": [("qsv.b_strategy", "0", "identity"), ("qsv.q", "30", "identity")],
    "s-bs1": [("qsv.b_strategy", "1", "identity"), ("qsv.q", "30", "identity")],
    "s-ab1": [("qsv.adaptive_b", "1", "identity"), ("qsv.b_strategy", "1", "identity"), ("qsv.q", "30", "identity")],
    "s-p1": [("qsv.preset", "1", "identity"), ("qsv.q", "30", "identity")],
    "s-p4": [("qsv.preset", "4", "identity"), ("qsv.q", "30", "identity")],
    "s-q24": [("qsv.q", "24", "identity")],
    "s-q36": [("qsv.q", "36", "identity")],
}
SCREEN_ENCODES = {"s-bs0": (60000000, 8000.0), "s-bs1": (30000000, 4000.0), "s-ab1": (30000000, 4000.0), "s-p1": (63000000, 8400.0),
                  "s-p4": (60000000, 8000.0), "s-q24": (66000000, 8800.0), "s-q36": (54000000, 7200.0)}
SCREEN_VERDICTS = [
    dict(kind="screen_verdict", setting_id="qsv.b_strategy", verdict="HONOURED", magnitude_pct=20.3, noise_floor_pct=2.77, cells=["s-bs0", "s-bs1"]),
    dict(kind="screen_verdict", setting_id="qsv.adaptive_b", verdict="INERT", magnitude_pct=0.0, base_setting_id="qsv.b_strategy", base_value="1",
         noise_floor_pct=2.77, cells=["s-ab1", "s-bs1"]),
    dict(kind="screen_verdict", setting_id="qsv.preset", verdict="HONOURED", magnitude_pct=5.01, noise_floor_pct=2.77, cells=["s-p1", "s-p4"]),
    dict(kind="admissibility_verdict", setting_id="qsv.q", test="opens", verdict="ADMISSIBLE", cells=["s-p4"]),
    dict(kind="admissibility_verdict", setting_id="qsv.q", test="monotone", verdict="ADMISSIBLE", cells=["s-q24", "s-p4", "s-q36"]),
]
VIEWING_CELLS = {"g-a": (qsv(30, "4", "0", False), 52000000, 6900.0), "g-b": (qsv(34, "4", "0", False), 44000000, 5900.0),
                 "g-i": (qsv(30, "1", "1", False), 36000000, 4800.0)}

# the mandatory path: the base arm at every in-range rung on every member; the candidate arm-b at two rungs on two windows,
# plus its failed cell; the incumbent at its pinned anchor on every member
ENCODE_CELLS = ([cell(f"c-a{r}-{w}", w, "reference", qsv(r, "4", "0")) for r in IN_RANGE for w in MEMBERS]
                + [cell(f"c-b{r}-{w}", w, "reference", qsv(r, "4", "1")) for r in (24, 30) for w in ("tng", "parks")]
                + [cell("c-b36-tng", "tng", "reference", qsv(36, "4", "1"))]
                + [cell(f"c-i30-{w}", w, "reference", qsv(30, "1", "1")) for w in MEMBERS])
FAILED = "c-b36-tng"


def encode_q(c):
    return int(dict((s, v) for s, v, _ in c.settings)["qsv.q"])


ENCODE_RECORDS = ([encode(c.cell_key, 34000000, 17000.0, kept=True) if c.cell_key.startswith("c-i30-")
                   else encode(c.cell_key, 60000000, 30000.0 - 400.0 * encode_q(c), kept=True)
                   for c in ENCODE_CELLS if c.cell_key != FAILED]
                  + [{"kind": "failure", "cell_key": FAILED, "at": "2026-09-02T14:00",
                      "stderr": "[av1_qsv @ 0x...] Error initializing the encoder: unsupported (-3)", "rc": 218}])
STEPS_A24 = [dict(scoring_step="rescale_ref", seconds=20.1, cores_busy=2.0, gpu_mean=0.0, gpu_max=0.0),
             dict(scoring_step="libvmaf", seconds=51.4, cores_busy=11.0, gpu_mean=15.9, gpu_max=22.0)]
SCORE_RECORDS = [score(c.cell_key, encode_q(c), STEPS_A24 if c.cell_key == "c-a24-tng" else ()) for c in ENCODE_CELLS if c.cell_key != FAILED]
TIME_CELLS = [cell(f"t-{w}", w, "source", qsv(24, "4", "0", False)) for w in ("tng", "parks", "tos")]
TIME_ENCODES = [encode("t-tng", 60000000, 8000.0), encode("t-parks", 30000000, 4000.0), encode("t-tos", 40000000, 5300.0, decode_path="software")]


def sample(workers, repeat_index, fps, wall_s, decode_path, is_warmup):
    return dict(workers=workers, repeat_index=repeat_index, fps=fps, wall_s=wall_s, decode_path=decode_path, is_warmup=is_warmup, noise_floor_pct=2.8, frames=1439)


TIMING_RECORDS = [
    dict(kind="timing", cell_key="t-tng", samples=[sample(1, 0, 640.0, 2.2, "hardware", True), sample(1, 1, 690.0, 2.1, "hardware", False), sample(1, 2, 700.0, 2.05, "hardware", False)]),
    dict(kind="timing", cell_key="t-parks", samples=[sample(1, 0, 700.0, 2.0, "hardware", True), sample(1, 1, 720.0, 2.0, "hardware", False)]),
    dict(kind="timing", cell_key="t-tos", samples=[sample(1, 0, 60.0, 24.0, "software", True), sample(1, 1, 62.0, 23.2, "software", False)]),
]

# ---------------------------------------------------------------- what ships

NVENC = [dict(setting_id="nvenc.preset", value="p2", role="identity"), dict(setting_id="nvenc.tune", value="uhq", role="identity")]
PROBE_REASON = "the HEVC probe is pinned to qp 14 so it reads the same on every host"
REMUX = dict(step="remux", provenance="fixed", decided_by="policy", reason="container rebuild only; no encoder decision")


def hevc(rc, anchor, value):
    return NVENC + [dict(setting_id="nvenc.rc", value=rc, role="identity"), dict(setting_id=anchor, value=value, role="identity")]


SHIPS = [
    (LANE, "media-01", [
        dict(step="quality-target-encode", encoder_unit_id=B580, provenance="measured", decided_by="measurement", content_class_id=CLASS, workers=1,
             evidence_query="SELECT ... FROM score WHERE ...", cites_json='["RESULTS §4g-ii"]',
             settings=[dict(setting_id="qsv.preset", value="4", role="identity"), dict(setting_id="qsv.b_strategy", value="0", role="identity"),
                       dict(setting_id="qsv.q", value="30", role="identity")]),
        dict(step="bitrate-target-encode", encoder_unit_id=B580, provenance="derived", decided_by="measurement", workers=1, cites_json='["RESULTS §18.6b"]',
             reason="-b:v is CEILING x HEADROOM; a whole-title VBV settles differently from a window",
             settings=[dict(setting_id="qsv.preset", value="4", role="identity"), dict(setting_id="qsv.b_strategy", value="0", role="identity"),
                       dict(setting_id="qsv.b_v", value="17600000", role="computed", from_constant="CEILING")]),
        dict(step="probe", encoder_unit_id=A4000, provenance="derived", decided_by="policy", reason=PROBE_REASON, workers=1,
             cites_json='["TDARR-TRANSCODE-PLAN.md rule 2"]', settings=hevc("constqp", "nvenc.qp", "14")),
        REMUX,
    ]),
    ("m4-ipad-gt1080p-sdr", "media-01", [
        dict(step="quality-target-encode", encoder_unit_id=A4000, provenance="derived", decided_by="measurement", workers=2, cites_json='["RESULTS §18.4"]',
             reason="budget interpolation between scored qp14 and qp17; confirmed by eye twice", settings=hevc("constqp", "nvenc.qp", "15")),
        dict(step="probe", encoder_unit_id=A4000, provenance="derived", decided_by="policy", reason=PROBE_REASON, workers=1, settings=hevc("constqp", "nvenc.qp", "14")),
        REMUX,
        dict(step="bitrate-target-encode", encoder_unit_id=A4000, provenance="derived", decided_by="measurement", reason="-b:v is CEILING x HEADROOM", workers=2,
             cites_json='["RESULTS §18.6b"]',
             settings=NVENC + [dict(setting_id="nvenc.rc", value="vbr", role="identity"), dict(setting_id="nvenc.b_v", value="17600000", role="computed", from_constant="CEILING")]),
    ]),
    ("kids-ipad-2d-animation-hdr", "media-01", [
        dict(step="quality-target-encode", encoder_unit_id=A4000, provenance="no-content", decided_by="policy", workers=2, cites_json='["RESULTS §4a"]',
             reason="the library has no HDR 2D animation; the SDR lane's value so the flow has no hole",
             settings=[dict(setting_id="nvenc.preset", value="p3", role="identity"), dict(setting_id="nvenc.cq", value="34", role="identity")]),
    ]),
]

# ---------------------------------------------------------------- the order


def steps():
    out = []
    for h in HOSTS:
        out.append(verb("/catalogue/add-host", h))
    out.append(verb("/catalogue/block-host", dict(host="htpc-01", fix="mount the sweep tree into tdarr-node, point the work root at it, use /ffmpeg/ffmpeg, then clear this")))
    out += [verb("/catalogue/add-unit", u) for u in UNITS]
    out += [verb("/catalogue/add-concept", dict(canonical_id=c, description=d)) for c, d in CONCEPTS]
    out += [verb("/catalogue/add-setting", s) for s in SETTINGS]
    out.append(run(plan("b580-inventory", "inventory", "media-01", "media-01", unit=None, cc=None, search=None, started_at="2026-08-24T09:00", **NODE),
                   "2026-08-24T09:00", "2026-08-24T09:20", TITLES))
    out += [verb("/catalogue/add-constant", c) for c in CONSTANTS]
    out += [verb("/catalogue/add-lane", l) for l in LANES]
    out += [verb("/catalogue/scope-constant", dict(name=n, lanes=ls)) for n, ls in SCOPES]
    out.append(verb("/catalogue/add-ladder", dict(ladder_id="hevc", codec="hevc", rungs=HEVC_RUNGS)))
    out.append(verb("/catalogue/add-ladder", dict(ladder_id="av1", codec="av1", rungs=AV1_RUNGS)))
    out += [verb("/sample/pin-window", dict(window_id=w, title_id=w, ss=ss, t=60.0, character=ch, selected_by="satavg+cuts v1", selection_score=sc, notes=n))
            for w, ss, ch, sc, n in PINS]
    out += [verb("/catalogue/author-chain", dict(lane=l, host=h, encoder_unit_id=u, vf_template=vf)) for l, h, u, vf in CHAINS]
    cuts = ([dict(kind="reference_set", reference_set_id="stage-1080p", geometry="1920x1080", pix_fmt="p010le", built_with="8.1.2-Jellyfin 0b0ea2d", built_at="2026-08-25")]
            + [dict(kind="cut", cut_id=f"{w}.ref", reference_set_id="stage-1080p", window_id=w, cut_kind="reference", chain_lane=LANE, chain_host="media-01",
                    chain_unit=B580, content_sha=f"sha-{w}-ref", bytes=2000000000, frames=1439) for w in WINDOWS]
            + [dict(kind="cut", cut_id=f"{w}.src", reference_set_id="stage-1080p", window_id=w, cut_kind="source", content_sha=f"sha-{w}-src",
                    bytes=200000000, frames=1439) for w in WINDOWS])
    out.append(run(plan("b580-materialise", "materialise", "media-01", "media-01-b580", windows=WINDOWS, cc=None, search=None, started_at="2026-08-25T09:00", **NODE),
                   "2026-08-25T09:00", "2026-08-25T11:00", cuts))
    out.append(run(plan("b580-verify", "verify", "media-01", "media-01", unit=None, cc=None, search=None, started_at="2026-08-26T09:00", **NODE),
                   "2026-08-26T09:00", "2026-08-26T09:30",
                   [dict(kind="cut_check", cut_id=f"{w}.ref", check_name="content", result="pass", checked_at="2026-08-26") for w in WINDOWS]))
    out.append(verb("/sample/define-class", dict(content_class_id=CLASS, name=CLASS, reference_set_id="stage-1080p",
                                                 description="1080p SDR live action, the m4-ipad-le1080p-sdr lane", lanes=[LANE], members=MEMBERS, strata=STRATA)))
    out.append(run(plan("b580-qsv-av1-screen", "screen", "media-01", "media-01-b580", windows=["tng"], search=None, started_at="2026-09-01T10:00",
                        cells=[cell(k, "tng", "reference", s) for k, s in SCREEN_CELLS.items()], **NODE),
                   "2026-09-01T10:00", "2026-09-01T11:03",
                   [encode(k, b, r) for k, (b, r) in SCREEN_ENCODES.items()] + SCREEN_VERDICTS))
    out.append(verb("/catalogue/add-scorer", dict(host="media-01-score", ffvship=["/usr/local/bin/FFVship"], score_ffmpeg=["/opt/jellyfin-ffmpeg/bin/ffmpeg"],
                                                  metric_backend="libvmaf_cuda", gpu_id=0, cache_dir="/mnt/data/sweep-score/cache")))
    out.append(run(plan("b580-viewing", "viewing", "media-01", "media-01-b580", windows=["tng"], search=None, started_at="2026-09-04T10:00",
                        cells=[cell(k, "tng", "reference", s) for k, (s, _, _) in VIEWING_CELLS.items()], **NODE),
                   "2026-09-04T10:00", "2026-09-04T10:10", [encode(k, b, r, kept=True) for k, (_, b, r) in VIEWING_CELLS.items()]))
    out.append(verb("/decision/record-viewing", dict(kind="pair", window_id="tng", cell_a="g-a", cell_b="g-b", viewed_on="iPad M4 13in", viewer="andy",
                                                     verdict="same", notes="not worth the size", viewed_at="2026-09-04")))
    out.append(verb("/decision/record-viewing", dict(kind="acceptance", lane=LANE, window_id="tng", cell_a="g-i", viewed_on="iPad M4 13in", viewer="andy",
                                                     verdict="acceptable", notes="what ships today, at q 30, is acceptable for the lane", viewed_at="2026-09-04")))
    arm = lambda arm_id, name, role, preset, bs, **more: dict(arm_id=arm_id, name=name, role=role, settings=[
        dict(setting_id="qsv.preset", value=preset), dict(setting_id="qsv.b_strategy", value=bs)], **more)
    out.append(verb("/search/author-search", dict(
        search_id=SEARCH, content_class_id=CLASS, encoder_unit_id=B580, anchor_setting_id="qsv.q", score_height=1548,
        notes="the first settings search; k = 3 factors, full factorial, preset swept",
        arms=[arm("arm-a", "cqp-preset4-bs0", "base", "4", "0"), arm("arm-b", "cqp-preset4-bs1", "candidate", "4", "1"),
              arm("arm-c", "cqp-preset1-bs0", "candidate", "1", "0"), arm("arm-i", "cqp-preset1-bs1-incumbent", "incumbent", "1", "1", anchor_value="30", accepted_by_viewing=2)],
        coarse_rungs=[12, 18, 24, 30, 36, 42],
        targets=[dict(metric="ssimulacra2", statistic="mean", target=t) for t in (75, 80, 85)])))
    out.append(run(plan("b580-qsv-av1-locate", "locate", "media-01", "media-01-b580", windows=MEMBERS, started_at="2026-09-02T00:00",
                        cells=[cell("l-a24-tng", "tng", "reference", qsv(24, "4", "0", False)), cell("l-a30-tng", "tng", "reference", qsv(30, "4", "0", False))], **NODE),
                   "2026-09-02T00:00", "2026-09-02T00:40", [encode("l-a24-tng", 70000000, 9300.0), encode("l-a30-tng", 52000000, 6900.0)]))
    out.append(run(plan("b580-qsv-av1", "encode", "media-01", "media-01-b580", windows=MEMBERS, started_at="2026-09-02T10:00", cells=ENCODE_CELLS, **NODE),
                   "2026-09-02T10:00", "2026-09-03T02:00", ENCODE_RECORDS))
    out.append(run(plan("b580-qsv-av1-score", "score", "media-01-score", "media-01-score", unit=None, cc=None, search=None, parent_run_id="b580-qsv-av1",
                        started_at="2026-09-03T02:30", **SCORER),
                   "2026-09-03T02:30", "2026-09-03T06:00", SCORE_RECORDS))
    out.append(run(plan("b580-qsv-av1-time", "time", "media-01", "media-01-b580", windows=MEMBERS, started_at="2026-09-03T10:00", cells=TIME_CELLS, **NODE),
                   "2026-09-03T10:00", "2026-09-03T12:00", TIME_ENCODES + TIMING_RECORDS))
    out.append(run(plan("m4-calibrate", "calibrate", "media-01", "media-01", unit=A4000, cc=None, search=None, started_at="2026-08-28T10:00", **NODE),
                   "2026-08-28T10:00", "2026-08-28T14:00"))
    out += [calibrate(n, "m4-calibrate", v, "2026-08-28") for n, v in (("HEADROOM", 0.98), ("BOUND", 20), ("RUNG_FACTOR", 0.8374), ("HOST_THRESHOLD", 10.0))]
    out.append(verb("/decision/exclude-route", dict(lane=LANE, host="eta", reason="eta is not yet measured on this class: the B580 column exists and eta has none")))
    out += [verb("/decision/ship", dict(lane=l, host=h, rows=rows)) for l, h, rows in SHIPS]
    out.append(verb("/search/set-shipping-arm", dict(search_id=SEARCH, arm_id="arm-a")))
    return out
