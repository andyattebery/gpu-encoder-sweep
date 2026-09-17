"""sweep/node/identity.py -- what this agent reports about itself: the artifact it runs, the harness version, the ffmpeg
build, sha and filters, the FFVship version when it is a scorer, the free space under its work root."""
import importlib.metadata
import os
import subprocess
import pathlib

from sweep.node import ffm

STAMP = "/etc/sweep-artifact"       # the images write two lines: <flavour>:<version>, and the git sha CI built from


def package_version():
    try:
        return importlib.metadata.version("gpu-encoder-sweep")
    except importlib.metadata.PackageNotFoundError:
        return "0+unknown"


def harness_version_of(version):
    """The git sha out of a version's +g<sha> part; a clean tag names its commit as well, so it stands as is."""
    if "+g" in version:
        return "g" + version.split("+g", 1)[1].split(".")[0]
    return version


def _ffvship_or_none(scorer_row):
    """The scorer's FFVship version, or None and a loud line when the tool says something this cannot read.

    A tool that changed its output must not stop an agent registering. It used to: the ParseError reached the top of
    `serve`, the process exited, and the container restarted forever -- eleven times in a few minutes on the first
    live deployment, with nothing in `sweep status` to say why. Reporting None leaves three visible signals instead:
    this line in the agent's log, a NULL ffvship_version in host_identity, and planning a score run refusing through
    artifact.scorer_build, which already says the identity reports no FFVship.
    """
    try:
        return ffm.ffvship_version(scorer_row["ffvship"])
    except (ffm.ParseError, ffm.ToolError, OSError, subprocess.SubprocessError) as e:
        print(f"sweep-node: cannot read FFVship's version, reporting none -- {e}", flush=True)
        return None


def identify(config, host_row, scorer_row, stamp_path=STAMP):
    version = package_version()
    stamp = pathlib.Path(stamp_path) if stamp_path else None
    if config.artifact_override:
        artifact, harness = config.artifact_override, harness_version_of(version)
    elif stamp is not None and stamp.is_file():
        lines = stamp.read_text().splitlines() + ["", ""]
        artifact, harness = lines[0].strip(), (lines[1].strip() or harness_version_of(version))
    else:
        artifact, harness = f"uvx:{version}", harness_version_of(version)
    build, sha, filters = ffm.tool_versions(host_row["ffmpeg"]) if host_row.get("ffmpeg") else ffm.tool_versions(scorer_row["score_ffmpeg"])
    ffvship = _ffvship_or_none(scorer_row) if scorer_row else None
    root = host_row["work_root"] if os.path.isdir(host_row["work_root"]) else os.getcwd()
    return {"artifact": artifact, "harness_version": harness, "ffmpeg_build": build, "ffmpeg_sha": sha, "ffmpeg_filters": list(filters),
            "ffvship_version": ffvship, "free_bytes": ffm.free_bytes(root)}
