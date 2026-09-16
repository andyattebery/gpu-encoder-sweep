"""sweep/hub/fleet.py -- the fleet catalogue as one document: what differs from the store, and what may still change.

A catalogue row is authored until a measurement points at it. After that its description is history -- the paths a
run composed its argv from, the device it addressed, the build a score recorded -- and changing it in place would
change what a finished measurement means. `referrers` is that question.

It is derived from the schema rather than from a list: the tables tagged `@class ROW` are the measurements, so a
table added later freezes by default instead of being forgotten. Stdlib only.
"""
import json

from sweep import model_check as mc
from sweep.hub.refusals import Refusal

GROUPS = ("hosts", "units", "scorers")

# what the document governs, column for column. `host.blocked` is deliberately absent: blocking is an operational
# act with its own two verbs, and a document carrying it would unblock a host on the next apply of a stale file.
COLUMNS = {
    "host": ("host", "ssh_host", "os", "machine", "work_root", "share_root", "ffmpeg", "local_view", "notes"),
    "encoder_unit": ("encoder_unit_id", "vendor", "card", "driver", "frontend", "codec"),
    "host_unit": ("host", "encoder_unit_id", "device"),
    "scorer": ("host", "ffvship", "score_ffmpeg", "metric_backend", "gpu_id", "cache_dir"),
}
OPTIONAL = {"host": ("ffmpeg", "local_view", "notes")}

# An agent writes a host_identity at its first heartbeat, before any run exists, and it describes the agent rather
# than where its files live. Without this exemption every host would freeze the moment its agent started -- which is
# exactly the window between deploying a runtime and authoring its row.
FREEZE_EXEMPT = {"host_identity"}


def row_referrer_tables(conn, table, exempt=True):
    """The ROW-class tables with a foreign key to `table`, sorted; the exempt ones dropped unless asked for."""
    tags, _ = mc.parse_tags()
    out = set()
    for t in mc.db_tables(conn):
        if tags.get(t, {}).get("class") != "ROW" or (exempt and t in FREEZE_EXEMPT):
            continue
        if any(to_table == table for _, to_table, _ in mc.foreign_keys(conn, t)):
            out.add(t)
    return tuple(sorted(out))


def referrers(conn, table, key):
    """{table: how many rows} for the measurements that point at this row -- what freezes it. Empty while it is
    still authored, and the caller may then change or remove it."""
    out = {}
    if table in ("host", "encoder_unit"):
        for t in row_referrer_tables(conn, table):
            for from_cols, to_table, to_cols in mc.foreign_keys(conn, t):
                if to_table != table:
                    continue
                where = " AND ".join(f"{c} = ?" for c in from_cols)
                (n,) = conn.execute(f"SELECT count(*) FROM {t} WHERE {where}", tuple(key[c] for c in to_cols)).fetchone()
                if n:
                    out[t] = out.get(t, 0) + n
    elif table == "host_unit":
        (n,) = conn.execute("SELECT count(*) FROM run WHERE host = ? AND encoder_unit_id = ?",
                            (key["host"], key["encoder_unit_id"])).fetchone()
        if n:
            out["run"] = n
    elif table == "scorer":
        # no table has a foreign key to scorer: what freezes it is a run that recorded the build it produced
        (n,) = conn.execute("SELECT count(*) FROM run WHERE host = ? AND scorer_build IS NOT NULL", (key["host"],)).fetchone()
        if n:
            out["run"] = n
    else:
        raise ValueError(f"not a fleet table: {table!r}")
    return out


# ---------------------------------------------------------------- the document against the store

def _rows(conn, table):
    """The managed columns of a table, as dicts."""
    cols = COLUMNS[table]
    return [dict(zip(cols, r)) for r in conn.execute(f"SELECT {', '.join(cols)} FROM {table}")]


def _row(entry, table):
    """One document entry as the table's row; a column the table allows to be NULL may be left out."""
    return {c: (entry.get(c) if c in OPTIONAL.get(table, ()) else entry[c]) for c in COLUMNS[table]}


def _by_key(entries, table, key, what):
    """{key: row}, refusing a document that names one thing twice -- otherwise the later entry wins in silence."""
    out = {}
    for e in entries:
        k = e[key]
        if k in out:
            raise Refusal(f"the document carries {what} {k!r} twice", "name each one once; two entries for one row is a merge nobody reviewed")
        out[k] = _row(e, table)
    return out


def _units(entries):
    """(identities by id, placements by (id, host)). One unit may sit in several hosts; two entries that disagree
    on what the unit IS are a different unit and refuse."""
    identities, placements = {}, {}
    for e in entries:
        uid, host = e["encoder_unit_id"], e["host"]
        ident = _row(e, "encoder_unit")
        if uid in identities and identities[uid] != ident:
            differ = ", ".join(c for c in COLUMNS["encoder_unit"] if identities[uid][c] != ident[c])
            raise Refusal(f"the document gives encoder_unit {uid!r} two identities, differing in {differ}",
                          "a unit is one (vendor, card, driver, frontend, codec); a different card or driver is a different id")
        if (uid, host) in placements:
            raise Refusal(f"the document puts encoder_unit {uid!r} in {host!r} twice", "name each placement once")
        identities[uid] = ident
        placements[(uid, host)] = _row(e, "host_unit")
    return identities, placements


def _compare(plan, group, want, have, name):
    for key, row in want.items():
        if key not in have:
            plan["created"][group].append(name(key))
            continue
        fields = {c: [have[key][c], row[c]] for c in row if have[key][c] != row[c]}
        if fields:
            plan["updated"][group].append({"name": name(key), "fields": fields})
        else:
            plan["unchanged"] += 1
    for key in have:
        if key not in want:
            plan["removed"][group].append(name(key))


def diff(conn, doc):
    """What `apply` would do: the creations, the updates carrying both values, the removals, and how many rows
    already agree. Pure -- it writes nothing, and refuses only what the document says about itself.

    A group the document leaves out is not managed by it: nothing of that kind is created, changed, removed or
    counted. An explicit empty list is a document that says there are none, and removes them all.
    """
    plan = {k: {g: [] for g in GROUPS} for k in ("created", "updated", "removed")} | {"unchanged": 0}
    if doc.get("hosts") is not None:
        _compare(plan, "hosts", _by_key(doc["hosts"], "host", "host", "host"),
                 {r["host"]: r for r in _rows(conn, "host")}, lambda k: k)
    if doc.get("units") is not None:
        identities, placements = _units(doc["units"])
        have_u = {r["encoder_unit_id"]: r for r in _rows(conn, "encoder_unit")}
        have_p = {(r["encoder_unit_id"], r["host"]): r for r in _rows(conn, "host_unit")}
        for key, row in placements.items():
            uid, _ = key
            if key not in have_p:
                plan["created"]["units"].append(_placement(key))
                continue
            fields = {c: [have_p[key][c], row[c]] for c in COLUMNS["host_unit"] if have_p[key][c] != row[c]}
            fields |= {c: [have_u[uid][c], identities[uid][c]] for c in COLUMNS["encoder_unit"] if have_u[uid][c] != identities[uid][c]}
            if fields:
                plan["updated"]["units"].append({"name": _placement(key), "fields": fields})
            else:
                plan["unchanged"] += 1
        for key in have_p:
            if key not in placements:
                plan["removed"]["units"].append(_placement(key))
    if doc.get("scorers") is not None:
        want = {}
        for e in doc["scorers"]:
            row = _row(e, "scorer")
            row["ffvship"] = json.dumps(e["ffvship"], separators=(",", ":"))
            row["score_ffmpeg"] = json.dumps(e["score_ffmpeg"], separators=(",", ":"))
            if e["host"] in want:
                raise Refusal(f"the document carries a scorer for {e['host']!r} twice", "name each one once; two entries for one row is a merge nobody reviewed")
            want[e["host"]] = row
        _compare(plan, "scorers", want, {r["host"]: r for r in _rows(conn, "scorer")}, lambda k: k)
    for kind in ("created", "removed"):
        for group in GROUPS:
            plan[kind][group].sort()
    for group in GROUPS:
        plan["updated"][group].sort(key=lambda u: u["name"])
    return plan


def _placement(key):
    return f"{key[0]}@{key[1]}"
