"""Helpers for the hub's tests. Not a test module: nothing here is named in the Makefile."""
from fastapi.testclient import TestClient

from sweep import model_check as mc
from sweep.hub.app import create_app
from sweep.hub.store import Store


def fixture_store():
    """A memory store holding the proof's fixture: every check clean, every table populated the way the campaign's is."""
    store = Store()
    store.conn.executescript(mc.FIXTURE)
    return store


def authored_tables(store):
    """{FILE table: its rows in primary-key order} -- what the API must reproduce exactly from the fixture."""
    return {t: [tuple(r.values()) for r in store.rows(t)] for t, tg in store.tags.items() if tg["class"] == "FILE"}


def client_for(store, token=None):
    return TestClient(create_app(store, None, token=token))


def post(client, path, body):
    """(status, text) of a POST; a refusal's text starts REFUSING, an ok reply is compact JSON."""
    r = client.post(path, json=body)
    return r.status_code, r.text
