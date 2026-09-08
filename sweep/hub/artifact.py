"""sweep/hub/artifact.py -- what each agent reported about itself, kept in host_identity, and what a plan pins from it.

An agent runs `identify` at start and on every change and sends the result in its heartbeat; the hub records a row
when it differs from the host's current one (free_bytes moves every time and is not a change). A plan copies the
current identity's artifact, harness version, ffmpeg build and sha into the run, so a run names the code its node
runs (x_run_artifact_not_reported); the claim then asks whether the node still reports it. Stdlib only.
"""
import json
from typing import NamedTuple

from sweep.hub import store as st
from sweep.hub.refusals import Refusal


class Identity(NamedTuple):
    artifact: str               # <flavour>:<version>: node-encode:0.0.3.dev2+gabc1234, node-score:..., uvx:..., hub:...
    harness_version: str        # the git sha, or the tag when the version is a clean tag
    ffmpeg_build: str           # the -version string
    ffmpeg_sha: str             # sha256 of the binary, computed where it runs
    ffmpeg_filters: tuple       # filter names the build has; the score planner looks for the backend's
    ffvship_version: str | None
    free_bytes: int


def _row(identity):
    row = identity._asdict()
    row["ffmpeg_filters"] = json.dumps(list(identity.ffmpeg_filters), separators=(",", ":"))
    return row


def current_identity(conn, host):
    """The identity the host's agent last reported, or None when it never did."""
    row = conn.execute("SELECT artifact, harness_version, ffmpeg_build, ffmpeg_sha, ffmpeg_filters, ffvship_version, free_bytes "
                       "FROM v_host_identity_current WHERE host = ?", (host,)).fetchone()
    if row is None:
        return None
    return Identity(row[0], row[1], row[2], row[3], tuple(json.loads(row[4])), row[5], row[6])


def record_identity(conn, host, identity, at):
    """Insert the identity when it differs from the host's current one (free_bytes aside); True when a row was written."""
    st.require(conn, "host", host=host)
    current = current_identity(conn, host)
    if current is not None and current._replace(free_bytes=0) == identity._replace(free_bytes=0):
        return False
    st.insert(conn, "host_identity", {"host": host, "reported_at": at, **_row(identity)})
    return True


def pins(identity):
    """The RunPlan fields a plan copies from the identity it was built for."""
    return {"artifact": identity.artifact, "harness_version": identity.harness_version,
            "ffmpeg_build": identity.ffmpeg_build, "ffmpeg_sha": identity.ffmpeg_sha}


def scorer_build(identity):
    """run.scorer_build: the binaries that scored -- FFVship's version and the ffmpeg build and sha -- never typed."""
    if identity.ffvship_version is None:
        raise Refusal("the identity reports no FFVship", "score on a host whose agent reports one: the node-score image")
    return f"FFVship {identity.ffvship_version} + {identity.ffmpeg_build}-{identity.ffmpeg_sha}"
