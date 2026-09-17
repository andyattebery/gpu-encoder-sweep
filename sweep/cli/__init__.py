"""sweep/cli -- the thin client. Every verb is a subcommand, every body field a flag; the hub's reply is printed as is.

    sweep [--hub URL] [--token TOKEN] <verb> [--from FILE] [--field value ...]

Exit 0 on an ok reply, 1 on a REFUSING one, 2 when the hub is unreachable. The hub and the token each resolve from
the flag, then the environment, then the file `sweep config` keeps them in (the hub falling back to
http://127.0.0.1:8000). --from FILE reads the body from JSON, and the flags override it.
The verb table is typed here once more than in the routers, and tests/test_cli.py proves the two equal, verb by
verb and field by field, against the app's OpenAPI document. Stdlib and httpx only.
"""
import argparse
import json
import os
import sys
from collections import OrderedDict, namedtuple

import httpx

from sweep import model_check as mc
from sweep.cli import config as cfg      # no cycle: config imports model_check and nothing of the CLI

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

DEFAULT_HUB = "http://127.0.0.1:8000"
LOCAL = {}                  # subcommands that reach no endpoint; VERBS is proven equal to the app's OpenAPI document


def verb(path, *flags):
    return Verb("POST", path, tuple((f, S) if isinstance(f, str) else f for f in flags))


def get(path):
    return Verb("GET", path, ())


VERBS = OrderedDict([
    ("apply", verb("/catalogue/apply", ("hosts", jsonarg), ("units", jsonarg), ("scorers", jsonarg), ("dry_run", B))),
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
    # no default from the environment: main resolves flag -> environment -> file -> localhost, and has to know
    # which of those each value came from so `sweep config` can report it
    ap.add_argument("--hub", metavar="URL")
    ap.add_argument("--token", metavar="TOKEN")
    sub = ap.add_subparsers(dest="verb", required=True, metavar="verb")
    c = sub.add_parser("config", help="show where --hub and --token come from; --save writes them to a 0600 file")
    c.add_argument("--save", action="store_true", help="write the resolved hub and token to the config file")
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


def resolve(env, args):
    """{"hub": (value, where), "token": (value|None, where)} -- the flag, then the environment, then the file."""
    saved = cfg.read(env)
    out = {}
    for name, key, fallback in (("hub", "SWEEP_HUB", DEFAULT_HUB), ("token", "SWEEP_TOKEN", None)):
        if getattr(args, name, None):
            out[name] = (getattr(args, name), "flag")
        elif env.get(key):
            out[name] = (env[key], "environment")
        elif saved.get(key):
            out[name] = (saved[key], str(cfg.path(env)))
        else:
            out[name] = (fallback, "default" if fallback else "unset")
    return out


def _config(env, where, args):
    """`sweep config`: what resolved and from where, the token as set/unset. `--save` writes the file at 0600."""
    if not args.save:
        for name in ("hub", "token"):
            value, source = where[name]
            shown = value if name == "hub" else ("set" if value else "unset")
            print(f"{name:<6} {shown or '':<26} {source}")
        return 0
    values = {"SWEEP_HUB": where["hub"][0] if where["hub"][1] != "default" else None, "SWEEP_TOKEN": where["token"][0]}
    if not any(values.values()):
        raise SystemExit(mc.refusing("there is no hub or token to save",
                                     "give --hub and --token on this call, or set SWEEP_HUB and SWEEP_TOKEN, then save"))
    path = cfg.write(env, values)
    print(f"wrote {path} (0600): {', '.join(k for k in cfg.KEYS if values.get(k))}")
    return 0


LOCAL["config"] = _config


def main(argv=None, transport=None, env=None):
    env = os.environ if env is None else env
    args = build_parser().parse_args(argv)
    # resolving before the LOCAL dispatch is deliberate: it reads the config file, so a file others can read
    # refuses on `config --save` too, which is what stops --save tightening a file by reading it first
    where = resolve(env, args)
    if args.verb in LOCAL:
        return LOCAL[args.verb](env, where, args)
    (hub, _), (token, _) = where["hub"], where["token"]
    v = VERBS[args.verb]
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    path = v.path
    for param in v.params:
        path = path.replace("{" + param + "}", getattr(args, param))
    try:
        with httpx.Client(base_url=hub, headers=headers, transport=transport, timeout=None if args.verb == "watch" else 60.0) as client:
            if args.verb == "watch":
                return _watch(client, path)
            r = client.request(v.method, path, json=body_of(args, v)) if v.method == "POST" else client.request(v.method, path)
    except httpx.TransportError as e:
        print(f"sweep: the hub at {hub} is unreachable: {e}", file=sys.stderr)
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
