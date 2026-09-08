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


from sweep.hub.store import stamp   # noqa: E402 -- the hub's clock, re-exported for the routers
