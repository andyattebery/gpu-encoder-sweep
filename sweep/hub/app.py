"""sweep/hub/app.py -- the hub: every verb a route, every refusal a 422 whose plain-text body starts REFUSING.

    sweep-hub            # SWEEP_STORE (a path, default hub.sqlite), SWEEP_BIND (host:port), SWEEP_TOKEN (the operator's bearer),
                         # SWEEP_AGENT_TOKENS (a JSON file {host: token}), SWEEP_REDIS (redis:// URL; FakeQueue without it),
                         # SWEEP_SHARE (the hub's view of temp/harness), SWEEP_FRAMES (per-frame values beside the store),
                         # SWEEP_SWEEP_S (the waiter's period, default 10)
"""
import os
import threading

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import PlainTextResponse

from sweep.hub import auth
from sweep.hub.api import agents, analysis, catalogue, decision, record, runs, sample, search
from sweep.hub.api.common import verb_name
from sweep.hub.refusals import Denied, Refusal, validation_refusal

ROUTERS = [catalogue.router, sample.router, search.router, decision.router, analysis.router, runs.router, agents.router, record.router]


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


def create_app(store, queue, token=None, agent_tokens=None, share=None, frames=None):
    app = FastAPI(title="sweep hub", description="the measurement harness's record: every write is a verb, checked in one transaction")
    tokens = auth.Tokens(token, dict(agent_tokens or {}))
    app.state.store, app.state.queue, app.state.tokens, app.state.share, app.state.frames = store, queue, tokens, share, frames

    if tokens.configured:
        @app.middleware("http")
        async def bearer(request, call_next):
            actor = tokens.actor(request.headers.get("authorization"))
            if actor is None:
                return PlainTextResponse(str(Refusal("no valid bearer token", "pass --token, or set SWEEP_TOKEN, to the hub's")), status_code=401)
            kind, host = actor
            route = f"{request.method} {request.url.path}"
            agent_route = "agents" in auth.route_tags(app.routes, request.scope)
            if agent_route and kind != "agent":
                return PlainTextResponse(str(Denied(f"{route} takes an agent's token", "set SWEEP_TOKEN on the agent to the token the hub holds for its host")), status_code=401)
            if not agent_route and kind != "operator":
                return PlainTextResponse(str(Denied(f"an agent's token does not open {route}", "the operator's token, SWEEP_TOKEN on the laptop, does")), status_code=401)
            request.state.agent_host = host
            return await call_next(request)

    for router in ROUTERS:
        app.include_router(router)

    @app.exception_handler(Denied)
    async def denied_response(request: Request, exc: Denied):
        return PlainTextResponse(str(exc), status_code=401)

    @app.exception_handler(Refusal)
    async def refusal_response(request: Request, exc: Refusal):
        return PlainTextResponse(str(exc), status_code=422)

    @app.exception_handler(RequestValidationError)
    async def validation_response(request: Request, exc: RequestValidationError):
        return PlainTextResponse(str(validation_refusal(verb_name(request), exc.errors(), body_fields(request))), status_code=422)

    return app


def main():
    import uvicorn

    from sweep.hub import wait
    from sweep.hub.queue import FakeQueue, RedisQueue
    from sweep.hub.store import Store

    store = Store(os.environ.get("SWEEP_STORE", "hub.sqlite"))
    tokens = auth.load_tokens(os.environ)
    queue = RedisQueue(os.environ["SWEEP_REDIS"]) if os.environ.get("SWEEP_REDIS") else FakeQueue()
    app = create_app(store, queue, token=tokens.operator, agent_tokens=tokens.agents,
                     share=os.environ.get("SWEEP_SHARE"), frames=os.environ.get("SWEEP_FRAMES"))
    threading.Thread(target=wait.run_forever, args=(store, queue, float(os.environ.get("SWEEP_SWEEP_S", "10"))), daemon=True).start()
    host, _, port = os.environ.get("SWEEP_BIND", "127.0.0.1:8000").rpartition(":")
    uvicorn.run(app, host=host or "127.0.0.1", port=int(port))
