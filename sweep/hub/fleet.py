"""sweep/hub/fleet.py -- the fleet catalogue as one document: what differs from the store, and what may still change.

A catalogue row is authored until a measurement points at it. After that its description is history -- the paths a
run composed its argv from, the device it addressed, the build a score recorded -- and changing it in place would
change what a finished measurement means. `referrers` is that question.

It is derived from the schema rather than from a list: the tables tagged `@class ROW` are the measurements, so a
table added later freezes by default instead of being forgotten. Stdlib only.
"""
from sweep import model_check as mc

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
