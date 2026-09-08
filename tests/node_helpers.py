"""Helpers for the agent's tests. Not a test module: nothing here is named in the Makefile.

hub_and_agent() builds an in-process hub whose one machine holds an encode runtime and a score runtime, both pointed
at the fake tools under tests/fake_tools, authors the smallest catalogue a run needs through the API, and returns the
pieces: the store, the client, the queue (a FakeQueue with a clock the test advances, or the queue given) and an
Agent whose HTTP client is the TestClient itself. The sample (titles, cuts) is made by the agent's own inventory and
adopt jobs, so the tests drive the same path the smoke will.
"""
import json
import pathlib
import tempfile

from sweep.hub.queue import FakeQueue
from sweep.node.agent import Agent
from sweep.node.config import Config
from tests.hub_helpers import client_for, post

TOOLS = pathlib.Path(__file__).parent / "fake_tools"
UNIT = "fake-iHD1-qsv-av1"
LANE, CLASS = "lane-a", "class-a"


def smoke_catalogue(client, root):
    """The hosts, the unit, the settings, the lane, the ladder, the chain and the scorer; nothing that needs a node."""
    work, work_score, share = root / "work", root / "work-score", root / "share"
    for d in (work, work_score, share):
        d.mkdir(parents=True, exist_ok=True)
    steps = [
        ("/catalogue/add-host", {"host": "enc", "ssh_host": "enc", "os": "linux", "machine": "box", "work_root": str(work), "share_root": str(share), "ffmpeg": str(TOOLS / "ffmpeg")}),
        ("/catalogue/add-host", {"host": "sco", "ssh_host": "enc", "os": "linux", "machine": "box", "work_root": str(work_score), "share_root": str(share), "local_view": str(work)}),
        ("/catalogue/add-host", {"host": "far", "ssh_host": "far", "os": "linux", "machine": "elsewhere", "work_root": str(root / "far"), "share_root": str(share)}),
        ("/catalogue/add-unit", {"encoder_unit_id": UNIT, "vendor": "intel", "card": "Fake", "driver": "iHD 1", "frontend": "qsv", "codec": "av1", "host": "enc", "device": "/dev/dri/by-path/pci-0000:00:00.0-render"}),
        ("/catalogue/add-concept", {"canonical_id": "quality_anchor", "description": "the anchor"}),
        ("/catalogue/add-concept", {"canonical_id": "rate_control_mode", "description": "the mode"}),
        ("/catalogue/add-concept", {"canonical_id": "preset", "description": "the preset"}),
        ("/catalogue/add-concept", {"canonical_id": "b_pyramid", "description": "B frames"}),
        ("/catalogue/add-setting", {"setting_id": "qsv.q", "flag": "-q:v", "frontend": "qsv", "kind": "quality_anchor", "subsystem": "rate_control", "value_type": "int", "range_lo": 1, "range_hi": 51,
                                    "is_generic": False, "roles": ["quality_anchor", "rate_control_mode"], "scope": [{"encoder_unit_id": UNIT, "applies": True, "default_is_measured": False}]}),
        ("/catalogue/add-setting", {"setting_id": "qsv.preset", "flag": "-preset", "frontend": "qsv", "kind": "ordinal", "subsystem": "other", "value_type": "enum", "is_generic": False,
                                    "enum_values": ["1", "4", "7"], "roles": ["preset"], "scope": [{"encoder_unit_id": UNIT, "applies": True, "default_value": "4", "default_is_measured": False}]}),
        ("/catalogue/add-setting", {"setting_id": "qsv.b_strategy", "flag": "-b_strategy", "frontend": "qsv", "kind": "option", "subsystem": "frame_types", "value_type": "enum", "is_generic": False,
                                    "enum_values": ["-1", "0", "1"], "roles": ["b_pyramid"], "scope": [{"encoder_unit_id": UNIT, "applies": True, "default_value": "-1", "default_is_measured": True}]}),
        ("/catalogue/add-lane", {"lane": LANE, "codec": "av1", "decision_rule": "incumbent", "input_dynamic_range": "sdr", "output_resolution": "1080p", "output_dynamic_range": "sdr",
                                 "hdr_handling": "n/a", "audio": "copy", "subtitles": "copy", "score_height": 1548, "bitrate_cap_binds": "never", "has_content": False, "steps": ["quality-target-encode"]}),
        ("/catalogue/add-ladder", {"ladder_id": "av1", "codec": "av1", "rungs": [20, 30, 40]}),
        ("/catalogue/author-chain", {"lane": LANE, "host": "enc", "encoder_unit_id": UNIT, "vf_template": "null"}),
        ("/catalogue/add-scorer", {"host": "sco", "ffvship": [str(TOOLS / "FFVship")], "score_ffmpeg": [str(TOOLS / "ffmpeg")], "metric_backend": "libvmaf_cuda", "gpu_id": 0, "cache_dir": str(root / "cache")}),
    ]
    for path, body in steps:
        status, text = post(client, path, body)
        assert status == 200, f"{path}: {text}"
    return {"work": work, "work_score": work_score, "share": share}


def title_files(root):
    """Two fake titles and their fake cuts, as files: the inventory probes the titles, the adopt hashes and copies the cuts."""
    lib, stage = root / "library", root / "stage"
    lib.mkdir(exist_ok=True)
    stage.mkdir(exist_ok=True)
    titles = {}
    for w in ("tng", "parks"):
        t = lib / f"{w}.S01E01.1080p.WEB-DL.mkv"
        t.write_bytes(w.encode() * 1000)
        (stage / f"{w}.ref.mkv").write_bytes((w + "ref").encode() * 5000)
        (stage / f"{w}.src.mkv").write_bytes((w + "src").encode() * 500)
        titles[w] = t
    return titles, stage


class Pieces:
    def __init__(self, store, client, queue, root, dirs, clock):
        self.store, self.client, self.queue, self.root, self.dirs, self.clock = store, client, queue, root, dirs, clock

    def agent(self, host):
        return Agent(Config(hub="http://testserver", token="", host=host), self.client, clock=self.clock)

    def run_row(self, run_id):
        return next(r for r in self.store.rows("run") if r["run_id"] == run_id)


def hub_and_agent(queue=None):
    """The hub, the catalogue, the two runtimes and their agents; the clock is the test's when the queue is the fake one."""
    from sweep.hub.store import Store
    root = pathlib.Path(tempfile.mkdtemp())
    now = [0.0]
    q = queue or FakeQueue(clock=lambda: now[0])
    store = Store()
    client = client_for(store, queue=q, share=root / "share", frames=root / "frames")
    dirs = smoke_catalogue(client, root)
    return Pieces(store, client, q, root, dirs, now)


def make_sample(pieces):
    """Titles through an inventory run, cuts through an adopt run, the class through the API: the sample a run needs."""
    titles, stage = title_files(pieces.root)
    agent = pieces.agent("enc")
    agent.start()
    status, text = post(pieces.client, "/runs/inventory", {"host": "enc", "library": "tv", "titles": [{"title_id": w, "path": str(p)} for w, p in titles.items()]})
    assert status == 200, text
    assert agent.serve_once() == json.loads(text)["run_id"]
    for w in titles:
        status, text = post(pieces.client, "/sample/pin-window", {"window_id": w, "title_id": w, "ss": 0.0, "t": 60.0})
        assert status == 200, text
    cuts = [{"window_id": w, "kind": k, "path": str(stage / f"{w}.{'ref' if k == 'reference' else 'src'}.mkv")} for w in titles for k in ("reference", "source")]
    status, text = post(pieces.client, "/runs/materialise", {"host": "enc", "encoder_unit_id": UNIT, "reference_set_id": "rs", "geometry": "1920x1080", "pix_fmt": "p010le",
                                                           "chain_lane": LANE, "chain_host": "enc", "chain_unit": UNIT, "cuts": cuts})
    assert status == 200, text
    assert agent.serve_once() == json.loads(text)["run_id"]
    for w in titles:
        status, text = post(pieces.client, "/sample/classify-cut", {"cut_id": f"{w}.ref", "check_name": "content", "reason": "adopted for the tests", "checked_at": "2026-09-07"})
        assert status == 200, text
    status, text = post(pieces.client, "/sample/define-class", {"content_class_id": CLASS, "name": "class a", "reference_set_id": "rs", "lanes": [LANE], "members": list(titles),
                                                              "strata": [{"stratum": "hd", "kind": "inventory", "definition": "t.width >= 1920", "min_windows": 1}]})
    assert status == 200, text
    return agent
