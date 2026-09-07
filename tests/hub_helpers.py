"""Helpers for the hub's tests. Not a test module: nothing here is named in the Makefile."""
from sweep import model_check as mc
from sweep.hub.store import Store


def fixture_store():
    """A memory store holding the proof's fixture: every check clean, every table populated the way the campaign's is."""
    store = Store()
    store.conn.executescript(mc.FIXTURE)
    return store


def authored_tables(store):
    """{FILE table: its rows in primary-key order} -- what the API must reproduce exactly from the fixture."""
    return {t: [tuple(r.values()) for r in store.rows(t)] for t, tg in store.tags.items() if tg["class"] == "FILE"}
