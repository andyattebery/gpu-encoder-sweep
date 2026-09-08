"""sweep/hub/api/common.py -- what every router shares: a body that forbids unknown fields, the store, the verb's name."""
from fastapi import Request
from pydantic import BaseModel, ConfigDict


class Body(BaseModel):
    """A request body: every field named, nothing extra. Every nested model subclasses it too, or the rule is lost."""
    model_config = ConfigDict(extra="forbid")


def get_store(request: Request):
    return request.app.state.store


def get_queue(request: Request):
    return request.app.state.queue


def verb_name(request: Request):
    return request.url.path.rstrip("/").rsplit("/", 1)[-1]


OK = {"ok": True}


def stamp():
    """The hub's clock, for every event it writes: one clock orders a run's log (x_run_state_disagrees_with_events reads max(at))."""
    import datetime as dt
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="microseconds")
