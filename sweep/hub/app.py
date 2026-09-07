"""sweep/hub/app.py -- the hub: every verb a route, every refusal a 422 whose plain-text body starts REFUSING.

    sweep-hub            # SWEEP_STORE (a path, default hub.sqlite), SWEEP_TOKEN (a bearer token), SWEEP_BIND (host:port)
"""
import os

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import PlainTextResponse

from sweep.hub.api import analysis, catalogue, decision, record, runs, sample, search
from sweep.hub.api.common import verb_name
from sweep.hub.refusals import Refusal, validation_refusal

ROUTERS = [catalogue.router, sample.router, search.router, decision.router, analysis.router, runs.router, record.router]


def api_routes(routes):
    """Every route with a path, descending into included routers (FastAPI wraps them and keeps the original)."""
    for route in routes:
        if getattr(route, "path", None) is not None:
            yield route
        else:
            inner = getattr(route, "original_router", None) or route
            yield from api_routes(getattr(inner, "routes", []))


def body_fields(request):
    """The field names of the route's body model, for the refusal that lists them: the route is found by path and method."""
    for route in api_routes(request.app.routes):
        if route.path == request.url.path and request.method in (getattr(route, "methods", None) or ()):
            body_field = getattr(route, "body_field", None)
            model = getattr(body_field, "type_", None) or getattr(getattr(body_field, "field_info", None), "annotation", None)
            return list(getattr(model, "model_fields", {}))
    return []


def create_app(store, queue, token=None):
    app = FastAPI(title="sweep hub", description="the measurement harness's record: every write is a verb, checked in one transaction")
    app.state.store, app.state.queue, app.state.token = store, queue, token

    if token:
        @app.middleware("http")
        async def bearer(request, call_next):
            if request.headers.get("authorization") != f"Bearer {token}":
                return PlainTextResponse(str(Refusal("no valid bearer token", "pass --token, or set SWEEP_TOKEN, to the hub's")), status_code=401)
            return await call_next(request)

    for router in ROUTERS:
        app.include_router(router)

    @app.exception_handler(Refusal)
    async def refusal_response(request: Request, exc: Refusal):
        return PlainTextResponse(str(exc), status_code=422)

    @app.exception_handler(RequestValidationError)
    async def validation_response(request: Request, exc: RequestValidationError):
        return PlainTextResponse(str(validation_refusal(verb_name(request), exc.errors(), body_fields(request))), status_code=422)

    return app


def main():
    import uvicorn

    from sweep.hub.queue import FakeQueue
    from sweep.hub.store import Store

    store = Store(os.environ.get("SWEEP_STORE", "hub.sqlite"))
    app = create_app(store, FakeQueue(), token=os.environ.get("SWEEP_TOKEN"))   # the Redis queue arrives with the agents (M2)
    host, _, port = os.environ.get("SWEEP_BIND", "127.0.0.1:8000").rpartition(":")
    uvicorn.run(app, host=host or "127.0.0.1", port=int(port))
