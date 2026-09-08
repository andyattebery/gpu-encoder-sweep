"""sweep/hub/auth.py -- who is speaking: the operator's token opens the operator's routes, an agent's token opens the
agent routes for its own host, and nothing else opens anything. Tokens come from the environment and a file the roles
write from the vault; none is ever in the store, which is exported."""
import json
import pathlib
from dataclasses import dataclass, field

from sweep import model_check as mc


@dataclass(frozen=True)
class Tokens:
    operator: str | None
    agents: dict = field(default_factory=dict)      # host -> token

    @property
    def configured(self):
        return bool(self.operator or self.agents)

    def actor(self, authorization):
        """("operator", None), ("agent", host), or None for a bearer the hub does not hold."""
        if not authorization or not authorization.startswith("Bearer "):
            return None
        token = authorization[len("Bearer "):]
        if self.operator and token == self.operator:
            return ("operator", None)
        for host, held in self.agents.items():
            if held == token:
                return ("agent", host)
        return None


def load_tokens(env):
    """SWEEP_TOKEN is the operator's; SWEEP_AGENT_TOKENS names a JSON file {host: token}; a named file that is missing refuses at start."""
    operator = env.get("SWEEP_TOKEN") or None
    agents = {}
    path = env.get("SWEEP_AGENT_TOKENS")
    if path:
        p = pathlib.Path(path)
        if not p.is_file():
            raise SystemExit(mc.refusing(f"SWEEP_AGENT_TOKENS names {path}, which does not exist",
                                         "write the file as {host: token} from the vault, or unset the variable"))
        agents = dict(json.loads(p.read_text()))
    return Tokens(operator, agents)


def route_tags(routes, scope):
    """The tags of the route the request will hit, so the middleware knows an agent route from the operator's before routing."""
    from starlette.routing import Match

    from sweep.hub.app import api_routes
    for route in api_routes(routes):
        match, _ = route.matches(scope)
        if match == Match.FULL:
            return tuple(getattr(route, "tags", None) or ())
    return ()
