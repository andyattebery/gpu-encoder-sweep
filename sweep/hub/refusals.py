"""sweep/hub/refusals.py -- the one form every refusal takes, composed from the schema, never invented here.

`REFUSING: <what> -- <fix>`. The fix for a firing check is the schema's `-- @fix` line; the fix for a named
CHECK constraint is in CONSTRAINT_FIXES, keyed by the name the schema gives it; an enum CHECK, a NOT NULL,
a UNIQUE, a foreign key and a STRICT type error are read off SQLite's own message. Stdlib only.
"""
import re
import sqlite3

from sweep import model_check as mc


class Refusal(Exception):
    def __init__(self, what, fix):
        super().__init__(mc.refusing(what, fix))
        self.what, self.fix = what, fix


# every named CHECK in sweep/schema.sql -> (what was refused, the fix); test_every_named_constraint_has_a_fix keeps it complete
CONSTRAINT_FIXES = {
    "setting_generic_has_no_frontend": ("a generic setting that names a frontend, or a frontend's setting that names none",
                                        "give --frontend for an encoder's option, or --generic for an ffmpeg-wide one, not both"),
    "constant_derived_has_inputs_and_precision": ("a derived constant without its inputs and precision",
                                                  "give --inputs-json and --precision; a derived number says what it came from"),
    "constant_policy_has_reason": ("a policy constant with no reason", "give --reason: why this value, and what bounds it"),
    "constant_value_iff_not_measured": ("a measured constant with a typed value, or a derived or policy one without",
                                        "a measured constant gets its value from calibrate; a derived or policy one is typed here with --value"),
    "lane_target_has_score_target": ("a target-bound lane without a score target, or a score target under another rule",
                                     "give --score-target with decision_rule target, and not otherwise"),
    "lane_cap_names_constant": ("a cap-bound lane with no cap constant", "give --bitrate-cap-constant, the CEILING the lane is capped by"),
    "lane_cap_binds_iff_constant": ("a cap that binds with no constant, or a constant on a lane whose cap never binds",
                                    "bitrate_cap_binds never goes with no constant; rarely or always go with one"),
    "title_dv_has_profile": ("a Dolby Vision title with no profile, or a profile on another dynamic range",
                             "dv_profile goes with dynamic_range dv and with nothing else"),
    "stratum_character_has_share": ("a character stratum with no share estimate, or a share on an inventory or quantile stratum",
                                    "give share_estimate on a character stratum only; it is judgement and says so"),
    "cut_reference_names_chain": ("a reference cut not naming its chain by lane, host and unit, or a source cut naming one",
                                  "a reference cut carries all three chain parts; a source cut none"),
    "cut_check_classified_has_reason": ("a classified cut check with no reason", "give --reason: what the check found and why the cut is kept"),
    "run_complete_is_verified": ("a run marked complete that was never verified",
                                 "the complete event follows the count and height verification; it sets verified_at"),
    "run_unit_by_stage": ("an inventory or verify run naming a unit, or another stage without one",
                          "inventory and verify use no encoder; every other stage names --unit"),
    "run_score_has_parent": ("a score run without its parent, or another stage with one",
                             "a score run is planned from the run whose cells it scores; no other stage has a parent"),
    "verdict_excluded_has_reason": ("an EXCLUDED verdict with no reason", "give the reason a setting is excluded; an exclusion is a written decision"),
    "admissibility_inadmissible_has_reason": ("an INADMISSIBLE verdict with no reason", "give the reason: the stderr that refused, or the reversal"),
    "arm_incumbent_is_pinned": ("an incumbent arm with no pinned anchor, or a base or candidate with one",
                                "give anchor_value on the incumbent arm only; the base and the candidates sweep the anchor"),
    "shipped_measured_has_class": ("a measured shipped value with no evidence class", "give --content-class: the class the value was measured on"),
    "shipped_policy_has_reason": ("a policy decision with no reason", "give --reason; a value decided by policy says why"),
    "shipped_remux_is_fixed": ("a remux step whose provenance is not fixed, or a fixed provenance on an encode step",
                               "a remux is fixed and only a remux is: no encoder decision"),
    "shipped_setting_constant_is_computed": ("a shipped setting naming a constant without the role computed",
                                             "a value from a constant is computed; give role computed with from_constant"),
    "viewing_pair_has_two_cells": ("a pair viewing with one encode, or an acceptance with two",
                                   "a pair names cell_a and cell_b; an acceptance names cell_a alone"),
    "viewing_acceptance_names_a_lane": ("an acceptance viewing with no lane, or a pair with one",
                                        "an acceptance says which lane's use it judges; a pair names none"),
    "viewing_verdict_fits_kind": ("a verdict that does not fit the viewing's kind",
                                  "a pair is a, b, same or unsure; an acceptance is acceptable, not_acceptable or unsure"),
    "host_unit_device_by_slot": ("a device addressed by render node", "give --device by PCI path or a stable id; a render-node number inverted twice"),
    "published_names_one_thing": ("a publish naming both an encode and a cut, or an encode without its run, or neither",
                                  "a published file is one product of the record: an encode with its run and cell, or a cut"),
}

# (table, column) -> the fix, where `give --column` is not enough
NOT_NULL_FIXES = {
    ("host", "machine"): "give --machine: the box this runtime is on; the quiet-box rule is per machine",
    ("host", "share_root"): "give --share-root: the share in this host's spelling",
    ("run", "artifact"): "a plan names the artifact it was built for; the agent reports it",
    ("timing", "frames"): "count the leg's frames; exit status is never the evidence",
    ("cut", "content_sha"): "hash the decoded frames, never the container",
}

# table -> the fix for a UNIQUE or PRIMARY KEY collision; the default says a FILE row is never overwritten
UNIQUE_FIXES = {
    "host": "a host row is never overwritten; block-host or unblock-host change it, and a second runtime on the box is a second host",
    "encoder_unit": "a unit is one (vendor, card, driver, frontend, codec); add-unit with the existing id puts it on another host",
    "host_unit": "the unit is already on that host",
    "ladder": "one ladder per codec, never per host; add-ladder once",
    "chain": "one chain per (lane, host, unit); author-chain once",
    "window": "a pinned window is never re-scanned; pin-window once per window_id",
    "content_class": "a class name is unique; define-class under another name",
    "shipped": "one value per (lane, host, step); ship every step of a (lane, host) in one call, once",
    "routing_exclusion": "one exclusion per (lane, host)",
    "viewing_verdict": "a viewing is recorded once",
}
UNIQUE_DEFAULT = "a FILE row is never overwritten; refuse, or add a new one under another key"

# table -> the verb that creates its rows, for `require`
ADD_VERB = {
    "host": "add-host", "encoder_unit": "add-unit", "host_unit": "add-unit", "canonical_concept": "add-concept",
    "setting": "add-setting", "setting_enum_value": "add-setting", "constant": "add-constant", "constant_scope": "scope-constant",
    "lane": "add-lane", "lane_step": "add-lane", "ladder": "add-ladder", "ladder_rung": "add-ladder", "chain": "author-chain",
    "scorer": "add-scorer", "title": "an inventory run", "window": "pin-window", "reference_set": "a materialise run",
    "cut": "a materialise run", "content_class": "define-class", "content_class_lane": "define-class",
    "content_class_member": "define-class", "search": "author-search", "arm": "author-search", "run": "a planned run",
    "cell": "a planned run", "viewing_verdict": "record-viewing", "shipped": "ship",
}


def named_constraints(sql=None):
    """The CONSTRAINT names the schema gives its logic CHECKs, in order."""
    return [name for name, _ in mc.check_constraints(sql) if name]


def check_refusal(name, description, rows, fix, others):
    """A firing check: the check's name and text, one row as the example, the other firing checks, the schema's fix."""
    what = f"{name}: {description}, e.g. {rows[0]!r}"
    if others:
        what += f" ({', '.join(others)} also fire{'s' if len(others) == 1 else ''})"
    return Refusal(what, fix)


def integrity_refusal(err):
    """SQLite's IntegrityError, read into the form: a named CHECK by its fix, the rest by what the message names."""
    msg = str(err)
    m = re.fullmatch(r"CHECK constraint failed: (\w+)", msg)
    if m and m.group(1) in CONSTRAINT_FIXES:
        return Refusal(*CONSTRAINT_FIXES[m.group(1)])
    m = re.fullmatch(r"CHECK constraint failed: (\w+) IN \((.*)\)", msg)
    if m:
        values = [v.strip().strip("'") for v in m.group(2).split(",")]
        return Refusal(f"{m.group(1)} must be one of {', '.join(values)}", "give one of them")
    m = re.fullmatch(r"NOT NULL constraint failed: (\w+)\.(\w+)", msg)
    if m:
        table, col = m.groups()
        return Refusal(f"{table}.{col} is missing", NOT_NULL_FIXES.get((table, col), f"give --{col.replace('_', '-')}"))
    m = re.fullmatch(r"UNIQUE constraint failed: (.+)", msg)
    if m:
        cols = [c.strip() for c in m.group(1).split(",")]
        table = cols[0].split(".")[0]
        names = ", ".join(c.split(".", 1)[1] for c in cols)
        return Refusal(f"{table} already has a row with that {names}", UNIQUE_FIXES.get(table, UNIQUE_DEFAULT))
    if msg.startswith("FOREIGN KEY constraint failed"):
        return Refusal("a row this one refers to does not exist", "add the referenced row first; the verbs check references by name before writing")
    m = re.fullmatch(r"cannot store (\w+) value in (\w+) column (\w+)\.(\w+)", msg)
    if m:
        given, wanted, table, col = m.groups()
        return Refusal(f"{table}.{col} must be an {wanted}, not {given}" if wanted[0] in "AEIOU" else f"{table}.{col} must be a {wanted}, not {given}",
                       f"give --{col.replace('_', '-')} as {wanted.lower()}")
    return Refusal(msg, "see sweep/schema.sql for the rule that refused it")


def validation_refusal(verb, errors, fields):
    """A body pydantic refused, in the form: the first error, named by its path and the flag that supplies it."""
    if not errors:
        return Refusal(f"{verb}: the body is not a JSON object", "post one; the CLI builds it from the flags")
    e = errors[0]
    loc = [l for l in e.get("loc", ()) if l != "body"]
    kind = e.get("type", "")
    if kind in ("json_invalid", "model_attributes_type", "dict_type", "model_type") or not loc:
        return Refusal(f"{verb}: the body is not a JSON object", "post one; the CLI builds it from the flags")
    path = "".join(f"[{l}]" if isinstance(l, int) else (f".{l}" if i else str(l)) for i, l in enumerate(loc))
    flag = "--" + str(loc[0]).replace("_", "-")
    if kind == "extra_forbidden":
        return Refusal(f"{verb} does not take {path!r}", "the fields are: " + ", ".join(fields))
    if kind == "missing":
        return Refusal(f"{verb} needs {path!r}", f"give {flag}")
    return Refusal(f"{verb}: {path} {e.get('msg', 'is invalid')}", f"give {flag} as the body schema says; GET /openapi.json describes it")
