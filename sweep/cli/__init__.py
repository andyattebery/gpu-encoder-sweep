"""sweep/cli -- the thin client. Every verb is a subcommand, every body field a flag; the hub's reply is printed as is.

    sweep [--hub URL] [--token TOKEN] <verb> [--from FILE] [--field value ...]

Exit 0 on an ok reply, 1 on a REFUSING one, 2 when the hub is unreachable. --hub defaults to SWEEP_HUB, then
http://127.0.0.1:8000; --token to SWEEP_TOKEN. --from FILE reads the body from JSON, and the flags override it.
The verb table is typed here once more than in the routers, and tests/test_cli.py proves the two equal, verb by
verb and field by field, against the app's OpenAPI document. Stdlib and httpx only.
"""
import argparse
import json
import os
import sys
from collections import OrderedDict, namedtuple

import httpx

Verb = namedtuple("Verb", "method path flags local params", defaults=((), ()))   # flags: the body's fields in order; local: the CLI's own options; params: the path's


def boolean(text):
    if text.lower() in ("true", "1", "yes"):
        return True
    if text.lower() in ("false", "0", "no"):
        return False
    raise argparse.ArgumentTypeError(f"{text!r} is not true or false")


def csv(text):
    return [t for t in text.split(",") if t]


def ints(text):
    return [int(t) for t in csv(text)]


def nullable(parse):
    def inner(text):
        return None if text.lower() in ("null", "none") else parse(text)
    return inner


def jsonarg(text):
    return json.loads(text)


S, I, F, B = str, nullable(int), nullable(float), boolean


def verb(path, *flags):
    return Verb("POST", path, tuple((f, S) if isinstance(f, str) else f for f in flags))


def get(path):
    return Verb("GET", path, ())


VERBS = OrderedDict([
    ("add-host", verb("/catalogue/add-host", "host", "ssh_host", "os", "machine", "work_root", "share_root", "ffmpeg", "local_view", "notes")),
    ("add-unit", verb("/catalogue/add-unit", "encoder_unit_id", "vendor", "card", "driver", "frontend", "codec", "host", "device")),
    ("add-concept", verb("/catalogue/add-concept", "canonical_id", "description")),
    ("add-setting", verb("/catalogue/add-setting", "setting_id", "flag", "frontend", "kind", "subsystem", "value_type", ("range_lo", F),
                         ("range_hi", F), ("is_generic", B), "notes", ("enum_values", csv), ("roles", csv), ("scope", jsonarg))),
    ("add-lane", verb("/catalogue/add-lane", "lane", "codec", "decision_rule", ("input_width_min", I), ("input_width_max", I),
                      "input_dynamic_range", "output_resolution", "output_dynamic_range", "hdr_handling", "audio", "subtitles",
                      ("score_target", F), ("score_height", I), "bitrate_cap_binds", "bitrate_cap_constant", ("has_content", B),
                      ("min_content_rate", F), ("steps", csv))),
    ("add-constant", verb("/catalogue/add-constant", "name", "unit", "provenance", ("value", F), "inputs_json", "precision", "reason", "cites_json")),
    ("scope-constant", verb("/catalogue/scope-constant", "name", ("lanes", csv))),
    ("add-ladder", verb("/catalogue/add-ladder", "ladder_id", "codec", ("rungs", ints))),
    ("author-chain", verb("/catalogue/author-chain", "lane", "host", "encoder_unit_id", "vf_template", "notes_ref")),
    ("add-scorer", verb("/catalogue/add-scorer", "host", ("ffvship", jsonarg), ("score_ffmpeg", jsonarg), "metric_backend", ("gpu_id", I), "cache_dir")),
    ("set-floor", verb("/catalogue/set-floor", "lane", ("min_content_rate", F))),
    ("block-host", verb("/catalogue/block-host", "host", "fix")),
    ("unblock-host", verb("/catalogue/unblock-host", "host")),
    ("pin-window", verb("/sample/pin-window", "window_id", "title_id", ("ss", F), ("t", F), "character", "selected_by", ("selection_score", F), "notes")),
    ("classify-cut", verb("/sample/classify-cut", "cut_id", "check_name", "reason", "checked_at")),
    ("define-class", verb("/sample/define-class", "content_class_id", "name", "reference_set_id", "description", ("lanes", csv), ("members", csv), ("strata", jsonarg))),
    ("author-search", verb("/search/author-search", "search_id", "content_class_id", "encoder_unit_id", "anchor_setting_id", ("score_height", I), "notes",
                           ("arms", jsonarg), ("coarse_rungs", ints), ("targets", jsonarg))),
    ("set-shipping-arm", verb("/search/set-shipping-arm", "search_id", "arm_id")),
    ("record-viewing", verb("/decision/record-viewing", "kind", "lane", "window_id", "cell_a", "cell_b", "viewed_on", "viewer", "verdict", "notes", "viewed_at")),
    ("ship", verb("/decision/ship", "lane", "host", ("rows", jsonarg))),
    ("exclude-route", verb("/decision/exclude-route", "lane", "host", "reason")),
    ("inventory", verb("/runs/inventory", "host", "library", ("titles", jsonarg))),
    ("materialise", verb("/runs/materialise", "host", "encoder_unit_id", "reference_set_id", "geometry", "pix_fmt", "chain_lane", "chain_host", "chain_unit",
                         ("cuts", jsonarg), ("adopt", B))),
    ("encode", verb("/runs/encode", "search_id", "content_class_id", "encoder_unit_id", "host", "stage", ("windows", csv), ("rungs", ints), ("arms", csv), ("cells", jsonarg))),
    ("score", verb("/runs/score", "run_id", "scorer", ("keep", B))),
    ("time", verb("/runs/time", "run_id", "lane", ("repeats", I))),
    ("publish", verb("/runs/publish", "run_id", ("cut_ids", csv), "via")),
    ("abandon", Verb("POST", "/runs/{run_id}/abandon", (("reason", S),), (), ("run_id",))),
    ("watch", Verb("GET", "/runs/{run_id}/watch", (), (), ("run_id",))),
    ("status", get("/runs/status")),
    ("check", get("/analysis/check")),
    ("export", Verb("GET", "/record/export", (), (("into", "write the record's files under this directory, removing what an earlier export wrote"),))),
])


def build_parser():
    ap = argparse.ArgumentParser(prog="sweep", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--hub", default=os.environ.get("SWEEP_HUB", "http://127.0.0.1:8000"))
    ap.add_argument("--token", default=os.environ.get("SWEEP_TOKEN"))
    sub = ap.add_subparsers(dest="verb", required=True, metavar="verb")
    for name, v in VERBS.items():
        p = sub.add_parser(name, help=v.path)
        for name_, help_ in v.local:
            p.add_argument("--" + name_, dest=name_, metavar="DIR", help=help_)
        for param in v.params:
            p.add_argument("--" + param.replace("_", "-"), dest=param, required=True, metavar="ID")
        if v.method == "POST":
            p.add_argument("--from", dest="from_file", metavar="FILE", help="the body as JSON; flags override it")
            for field, parse in v.flags:
                p.add_argument("--" + field.replace("_", "-"), dest=field, type=parse, default=argparse.SUPPRESS,
                               metavar="NULL|VALUE" if parse in (I, F) else "true|false" if parse is B else "VALUE")
    return ap


def body_of(args, v):
    body = {}
    if getattr(args, "from_file", None):
        with open(args.from_file) as f:
            body.update(json.load(f))
    for field, _ in v.flags:
        if hasattr(args, field):
            body[field] = getattr(args, field)
    return body


def main(argv=None, transport=None):
    args = build_parser().parse_args(argv)
    v = VERBS[args.verb]
    headers = {"Authorization": f"Bearer {args.token}"} if args.token else {}
    path = v.path
    for param in v.params:
        path = path.replace("{" + param + "}", getattr(args, param))
    try:
        with httpx.Client(base_url=args.hub, headers=headers, transport=transport, timeout=None if args.verb == "watch" else 60.0) as client:
            if args.verb == "watch":
                return _watch(client, path)
            r = client.request(v.method, path, json=body_of(args, v)) if v.method == "POST" else client.request(v.method, path)
    except httpx.TransportError as e:
        print(f"sweep: the hub at {args.hub} is unreachable: {e}", file=sys.stderr)
        return 2
    if args.verb == "export" and r.status_code == 200 and getattr(args, "into", None):
        from sweep.hub.export import write
        written = write(r.json()["files"], args.into)
        print(f"{len(written)} files under {args.into}")
        return 0
    print(r.text)
    return 1 if r.text.startswith("REFUSING") or r.status_code >= 400 else 0


def _watch(client, path):
    """Print each server-sent event's payload as it arrives; exit by the final state: 0 complete, 1 failed or abandoned."""
    last = None
    with client.stream("GET", path) as r:
        if r.status_code >= 400:
            print(r.read().decode())
            return 1
        for line in r.iter_lines():
            if line.startswith("data: "):
                print(line[6:])
                last = json.loads(line[6:])
    return 0 if last is not None and last.get("state") == "complete" else 1
