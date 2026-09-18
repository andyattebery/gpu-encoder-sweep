"""Helpers for the hub's tests. Not a test module: nothing here is named in the Makefile."""
import json

from fastapi.testclient import TestClient

from sweep import model_check as mc
from sweep.hub.app import create_app
from sweep.hub.queue import FakeQueue
from sweep.hub.store import Store


def fixture_store():
    """A memory store holding the proof's fixture: every check clean, every table populated the way the campaign's is."""
    store = Store()
    store.conn.executescript(mc.FIXTURE)
    return store


def authored_tables(store):
    """{FILE table: its rows in primary-key order} -- what the API must reproduce exactly from the fixture."""
    return {t: [tuple(r.values()) for r in store.rows(t)] for t, tg in store.tags.items() if tg["class"] == "FILE"}


def client_for(store, token=None, agent_tokens=None, share=None, frames=None, queue=None, hub_host=None):
    return TestClient(create_app(store, queue or FakeQueue(), token=token, agent_tokens=agent_tokens, share=share, frames=frames, hub_host=hub_host))


def post(client, path, body):
    """(status, text) of a POST; a refusal's text starts REFUSING, an ok reply is compact JSON."""
    r = client.post(path, json=body)
    return r.status_code, r.text


def fleet_document(store):
    """The store's own fleet in the document's shape -- what `apply` must treat as a no-op."""
    from sweep.hub import fleet
    units = {u["encoder_unit_id"]: u for u in store.rows("encoder_unit")}
    return {
        "hosts": [{k: r[k] for k in fleet.COLUMNS["host"]} for r in store.rows("host")],
        "units": [dict({k: units[p["encoder_unit_id"]][k] for k in fleet.COLUMNS["encoder_unit"]},
                       host=p["host"], device=p["device"]) for p in store.rows("host_unit")],
        "scorers": [dict({k: r[k] for k in ("host", "metric_backend", "gpu_id", "cache_dir")},
                         ffvship=json.loads(r["ffvship"]), score_ffmpeg=json.loads(r["score_ffmpeg"]))
                    for r in store.rows("scorer")],
    }
