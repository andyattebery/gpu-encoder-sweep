"""sweep/hub/fleet.py -- the fleet catalogue as one document: what differs from the store, and what may still change.

A catalogue row is authored until a measurement points at it. After that its description is history -- the paths a
run composed its argv from, the device it addressed, the build a score recorded -- and changing it in place would
change what a finished measurement means. `referrers` is that question.

It is derived from the schema rather than from a list: the tables tagged `@class ROW` are the measurements, so a
table added later freezes by default instead of being forgotten. Stdlib only.
"""
import json

from sweep import model_check as mc
from sweep.hub import store as st
from sweep.hub.refusals import Refusal

GROUPS = ("hosts", "units", "scorers")

# what the document governs, column for column. `host.blocked` is deliberately absent: blocking is an operational
# act with its own two verbs, and a document carrying it would unblock a host on the next apply of a stale file.
COLUMNS = {
    "host": ("host", "ssh_host", "os", "machine", "work_root", "ffmpeg", "local_view", "notes"),
    "encoder_unit": ("encoder_unit_id", "vendor", "card", "driver", "frontend", "codec"),
    "host_unit": ("host", "encoder_unit_id", "device"),
    "scorer": ("host", "ffvship", "score_ffmpeg", "metric_backend", "gpu_id", "cache_dir"),
}
OPTIONAL = {"host": ("ffmpeg", "local_view", "notes")}

# the columns that stay free once a measurement points at the row: an annotation and how to reach the box, neither of
# which is in any argv, any plan or any record. Everything else is frozen, including a column added to the schema
# later -- the direction that fails safe.
FREE = {"host": ("notes", "ssh_host")}
FROZEN_FIX = {
    "host": "a measurement keeps the paths it ran on: add a host row carrying the new value and plan against that, or "
            "abandon what names this one; notes and ssh_host stay free",
    "host_unit": "a measurement keeps the device it addressed: add the card as its own placement, or abandon the runs that used this one",
    "scorer": "a score keeps the build it recorded: author the scorer on another host, or abandon the score runs that used this one",
    "encoder_unit": "a unit is one (vendor, card, driver, frontend, codec); a different card or driver is a different id, not an edit",
}

# An agent writes a host_identity at its first heartbeat, before any run exists, and it describes the agent rather
# than where its files live. Without this exemption every host would freeze the moment its agent started -- which is
# exactly the window between deploying a runtime and authoring its row.
FREEZE_EXEMPT = {"host_identity"}


def _referring_tables(conn, table, klass=None, exempt=True):
    tags, _ = mc.parse_tags()
    out = set()
    for t in mc.db_tables(conn):
        if exempt and t in FREEZE_EXEMPT:
            continue
        if klass is not None and tags.get(t, {}).get("class") != klass:
            continue
        if any(to_table == table for _, to_table, _ in mc.foreign_keys(conn, t)):
            out.add(t)
    return tuple(sorted(out))


def row_referrer_tables(conn, table, exempt=True):
    """The ROW-class tables with a foreign key to `table`, sorted; the exempt ones dropped unless asked for."""
    return _referring_tables(conn, table, klass="ROW", exempt=exempt)


def _fk_counts(conn, table, key, tables):
    out = {}
    for t in tables:
        for from_cols, to_table, to_cols in mc.foreign_keys(conn, t):
            if to_table != table:
                continue
            where = " AND ".join(f"{c} = ?" for c in from_cols)
            (n,) = conn.execute(f"SELECT count(*) FROM {t} WHERE {where}", tuple(key[c] for c in to_cols)).fetchone()
            if n:
                out[t] = out.get(t, 0) + n
    return out


def blockers(conn, table, key):
    """{table: how many rows} for everything that points at this row, authored or measured. A row may not be removed
    while any of them stand -- unlike a field, which only a measurement freezes. Nothing points at a scorer today and
    this answers {} for one, without that being written down anywhere it could go stale."""
    return _fk_counts(conn, table, key, _referring_tables(conn, table))


def _names(refs):
    return ", ".join(f"{n} {t} row" + ("s" if n != 1 else "") for t, n in sorted(refs.items()))


def referrers(conn, table, key):
    """{table: how many rows} for the measurements that point at this row -- what freezes it. Empty while it is
    still authored, and the caller may then change or remove it."""
    out = {}
    if table in ("host", "encoder_unit"):
        out = _fk_counts(conn, table, key, row_referrer_tables(conn, table))
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


def _placement(key):
    return f"{key[0]}@{key[1]}"


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


def _normalise(doc):
    """The document as rows, or None per group where the document does not manage that kind."""
    hosts = _by_key(doc["hosts"], "host", "host", "host") if doc.get("hosts") is not None else None
    identities, placements = _units(doc["units"]) if doc.get("units") is not None else (None, None)
    scorers = _by_key(doc["scorers"], "scorer", "host", "a scorer for") if doc.get("scorers") is not None else None
    for row in (scorers or {}).values():
        row["ffvship"] = json.dumps(row["ffvship"], separators=(",", ":"))
        row["score_ffmpeg"] = json.dumps(row["score_ffmpeg"], separators=(",", ":"))
    return hosts, identities, placements, scorers


def diff(conn, doc):
    """What `apply` would do: the creations, the updates carrying both values, the removals, and how many rows
    already agree. Pure -- it writes nothing, and refuses only what the document says about itself.

    A group the document leaves out is not managed by it: nothing of that kind is created, changed, removed or
    counted. An explicit empty list is a document that says there are none, and removes them all.
    """
    plan = {k: {g: [] for g in GROUPS} for k in ("created", "updated", "removed")} | {"unchanged": 0}
    hosts, identities, placements, scorers = _normalise(doc)
    if hosts is not None:
        _compare(plan, "hosts", hosts, {r["host"]: r for r in _rows(conn, "host")}, lambda k: k)
    if placements is not None:
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
    if scorers is not None:
        _compare(plan, "scorers", scorers, {r["host"]: r for r in _rows(conn, "scorer")}, lambda k: k)
    for kind in ("created", "removed"):
        for group in GROUPS:
            plan[kind][group].sort()
    for group in GROUPS:
        plan["updated"][group].sort(key=lambda u: u["name"])
    return plan


# ---------------------------------------------------------------- making the store match

def _refuse_frozen(conn, table, key, name, fields, what):
    """A field a measurement's meaning rests on may not change once one points at the row."""
    locked = sorted(c for c in fields if c not in FREE.get(table, ()))
    if not locked:
        return
    refs = referrers(conn, table, key)
    if not refs:
        return
    col = locked[0]
    was, now = fields[col]
    rest = locked[1:]
    also = f", as {'does' if len(rest) == 1 else 'do'} {', '.join(rest)}" if rest else ""
    raise Refusal(f"{what} {name!r} {col} differs ({was!r} -> {now!r}){also}, and it is named by {_names(refs)}", FROZEN_FIX[table])


def _refuse_blocked(conn, table, key, name, what):
    blocks = blockers(conn, table, key)
    if blocks:
        raise Refusal(f"the document does not carry {what} {name!r}, which is named by {_names(blocks)}",
                      "put it back in the document, or remove what names it first; nothing is orphaned to make a document true")


def _unit_exists_and_agrees(conn, uid, ident):
    """True when the store already holds this unit with this identity; False when it is new. A disagreement refuses:
    a different card or driver is a different unit, and every measurement under the old id would silently move."""
    have = {r["encoder_unit_id"]: r for r in _rows(conn, "encoder_unit")}.get(uid)
    if have is None:
        return False
    differ = [c for c in COLUMNS["encoder_unit"] if have[c] != ident[c]]
    if differ:
        raise Refusal(f"encoder_unit {uid!r} is already in the store with another identity, differing in {', '.join(differ)}",
                      FROZEN_FIX["encoder_unit"])
    return True


def _cascade(conn, table, key):
    """Remove what the exempt tables hold about a row that is going: an agent's own report about a host, which it
    writes again at its next heartbeat if the host comes back."""
    for t in sorted(FREEZE_EXEMPT):
        for from_cols, to_table, to_cols in mc.foreign_keys(conn, t):
            if to_table == table:
                st.delete(conn, t, {f: key[c] for f, c in zip(from_cols, to_cols)})


def apply(conn, doc):
    """Make the store match the document, inside the caller's transaction; returns the plan it carried out.

    Creations and updates first, in foreign-key order, then removals in reverse -- so a row and what points at it are
    never both half-there. Every refusal is raised before the write it would have made, and the caller's transaction
    takes the rest back.
    """
    plan = diff(conn, doc)
    hosts, identities, placements, scorers = _normalise(doc)

    if hosts is not None:
        for name in plan["created"]["hosts"]:
            st.insert(conn, "host", dict(hosts[name], blocked=None))
        for u in plan["updated"]["hosts"]:
            _refuse_frozen(conn, "host", {"host": u["name"]}, u["name"], u["fields"], "host")
            st.update(conn, "host", {"host": u["name"]}, {c: hosts[u["name"]][c] for c in u["fields"]})

    if placements is not None:
        by_name = {_placement(k): k for k in placements}
        for name in plan["created"]["units"]:
            uid, host = by_name[name]
            st.require(conn, "host", host=host)
            if not _unit_exists_and_agrees(conn, uid, identities[uid]):
                st.insert(conn, "encoder_unit", identities[uid])
            st.insert(conn, "host_unit", placements[(uid, host)])
        for u in plan["updated"]["units"]:
            uid, host = by_name[u["name"]]
            _unit_exists_and_agrees(conn, uid, identities[uid])          # an identity difference refuses here
            _refuse_frozen(conn, "host_unit", {"host": host, "encoder_unit_id": uid}, u["name"], u["fields"],
                           "the placement of encoder_unit")
            st.update(conn, "host_unit", {"host": host, "encoder_unit_id": uid},
                      {c: placements[(uid, host)][c] for c in u["fields"]})

    if scorers is not None:
        for name in plan["created"]["scorers"]:
            st.require(conn, "host", host=name)
            st.insert(conn, "scorer", scorers[name])
        for u in plan["updated"]["scorers"]:
            _refuse_frozen(conn, "scorer", {"host": u["name"]}, u["name"], u["fields"], "the scorer on")
            st.update(conn, "scorer", {"host": u["name"]}, {c: scorers[u["name"]][c] for c in u["fields"]})

    for name in plan["removed"]["scorers"]:
        _refuse_blocked(conn, "scorer", {"host": name}, name, "the scorer on")
        st.delete(conn, "scorer", {"host": name})
    have_p = {_placement((r["encoder_unit_id"], r["host"])): (r["encoder_unit_id"], r["host"]) for r in _rows(conn, "host_unit")}
    for name in plan["removed"]["units"]:
        uid, host = have_p[name]
        key = {"host": host, "encoder_unit_id": uid}
        _refuse_blocked(conn, "host_unit", key, name, "the placement of encoder_unit")
        st.delete(conn, "host_unit", key)
        if not conn.execute("SELECT 1 FROM host_unit WHERE encoder_unit_id = ?", (uid,)).fetchone():
            _refuse_blocked(conn, "encoder_unit", {"encoder_unit_id": uid}, uid, "encoder_unit")
            st.delete(conn, "encoder_unit", {"encoder_unit_id": uid})     # nothing places it any more
    for name in plan["removed"]["hosts"]:
        _cascade(conn, "host", {"host": name})
        _refuse_blocked(conn, "host", {"host": name}, name, "host")
        st.delete(conn, "host", {"host": name})
    return plan
