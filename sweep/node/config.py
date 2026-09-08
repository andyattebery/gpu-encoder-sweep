"""sweep/node/config.py -- three environment variables, written by the role; everything else comes from the hub."""
from dataclasses import dataclass

from sweep import model_check as mc

REQUIRED = {"SWEEP_HUB": "the hub's URL", "SWEEP_TOKEN": "this host's token, the one the hub holds for it", "SWEEP_HOST": "the host row this agent acts for"}


@dataclass(frozen=True)
class Config:
    hub: str
    token: str
    host: str
    artifact_override: str | None = None    # SWEEP_ARTIFACT, for a runtime the images did not stamp

    @classmethod
    def from_env(cls, env):
        for name, what in REQUIRED.items():
            if not env.get(name):
                raise SystemExit(mc.refusing(f"{name} is not set", f"the role writes it: {what}"))
        return cls(env["SWEEP_HUB"], env["SWEEP_TOKEN"], env["SWEEP_HOST"], env.get("SWEEP_ARTIFACT") or None)
