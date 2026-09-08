"""sweep/node/ffm.py -- the tool layer: subprocesses with argv lists (never a shell), the -progress parser that demands
progress=end, the framemd5 content hash, the decode probe read from stderr and never the exit status, tool versions and
the binary's sha computed where it runs. Lifted from the archived harness where a line is cited; the reasons stay."""
import hashlib
import os
import shlex
import shutil
import subprocess


class ParseError(Exception):
    pass


class ToolError(Exception):
    """A tool that did not do what was asked: the command, the exit status and the tail of its stderr."""

    def __init__(self, what, cmd=(), rc=None, stderr=""):
        super().__init__(what)
        self.cmd, self.rc, self.stderr = list(cmd), rc, stderr


def as_cmd(spec):
    """A tool spec is a bare binary or a command prefix. A bare existing path is taken WHOLE (spaces and parentheses
    included -- shlex in POSIX mode destroys c:\\Program Files\\...); a multi-token spec is split, non-POSIX on Windows
    with the surviving quotes stripped (sweep.py:2077)."""
    if isinstance(spec, (list, tuple)):
        return list(spec)
    if os.path.isfile(spec):
        return [spec]
    if os.name == "nt":
        return [p.strip('"') for p in shlex.split(spec, posix=False)]
    return shlex.split(spec)


def run(argv, timeout=None, cwd=None):
    """The process, captured; the caller decides what its stderr and exit status mean."""
    return subprocess.run(list(argv), capture_output=True, text=True, errors="replace", timeout=timeout, cwd=cwd, check=False)


def parse_progress(text):
    """The final block of an ffmpeg -progress file: frames, size_bytes, out_time_s (and speed when given). The last value
    seen wins; progress=end is required; fps is deliberately NOT read -- the final block reports fps=0.00 (sweep.py:1013)."""
    if not text.strip():
        raise ParseError("empty -progress output")
    seen = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or "=" not in line:
            continue
        k, _, v = line.partition("=")
        seen[k.strip()] = v.strip()
    if seen.get("progress") != "end":
        raise ParseError(f"-progress did not reach progress=end (saw {seen.get('progress')!r})")
    try:
        frames, size_bytes, out_time_us = int(seen["frame"]), int(seen["total_size"]), int(seen["out_time_us"])
    except KeyError as exc:
        raise ParseError(f"-progress missing required key {exc}") from exc
    except ValueError as exc:
        raise ParseError(f"-progress value not an integer: {exc}") from exc
    if frames <= 0:
        raise ParseError(f"-progress reported frame={frames}")
    if size_bytes <= 0:
        raise ParseError(f"-progress reported total_size={size_bytes}")
    out = {"frames": frames, "size_bytes": size_bytes, "out_time_s": out_time_us / 1_000_000.0}
    speed = seen.get("speed", "").rstrip("x")
    if speed and speed != "N/A":
        try:
            out["speed"] = float(speed)
        except ValueError:
            pass
    return out


def progress_frames(text):
    """Frames out of a -progress file and nothing else: a null-terminated leg reports total_size=N/A, which parse_progress
    refuses, and a leg that ran perfectly must not read as failed (sweep.py:5674)."""
    frames = 0
    for line in text.splitlines():
        k, _, v = line.partition("=")
        if k.strip() == "frame" and v.strip().isdigit():
            frames = int(v.strip())
    return frames


def content_hash_of(framemd5_stdout):
    """(sha256[:32] of the frame rows, the frame count): the DECODED FRAMES, never the container's bytes (sweep.py:3838)."""
    rows = [l for l in framemd5_stdout.splitlines() if l and not l.startswith("#")]
    if not rows:
        raise ParseError("framemd5 produced no frame rows")
    return hashlib.sha256("\n".join(rows).encode()).hexdigest()[:32], len(rows)


def content_hash(ffmpeg, argv):
    """Run the hash argv the plan carries (build.hash_argv) with the tool; (sha, frames)."""
    proc = run([*as_cmd(ffmpeg), *argv], timeout=3600)
    if proc.returncode != 0:
        raise ToolError(f"framemd5 failed: {proc.stderr.strip()[:200]}", proc.args, proc.returncode, proc.stderr)
    return content_hash_of(proc.stdout)


NO_HW_DECODE = ("No support for codec", "Failed setup for format")     # ffmpeg's two lines for "this device has no decoder for this codec"


def decode_path_of(stderr, rc):
    """hardware or software, from the probe's stderr: a device with no decoder falls back to software and RETURNS 0, so the
    exit status says nothing; no signature and a non-zero status is a probe that did not run, never a capability answer
    (sweep.py:2377, adm-absence-is-not-a-result)."""
    if any(sig in stderr for sig in NO_HW_DECODE):
        return "software"
    if rc == 0:
        return "hardware"
    raise ToolError(f"the decode probe exited {rc} with no recognisable result; that is not a capability answer: {stderr.strip()[:300]}", (), rc, stderr)


def decode_probe(ffmpeg, argv):
    proc = run([*as_cmd(ffmpeg), *argv], timeout=300)
    return decode_path_of(proc.stderr, proc.returncode)


def build_of(version_stdout):
    """The build string after `ffmpeg version` on the first line: 8.1.2-Jellyfin. It cannot identify a patched build; the sha can."""
    first = version_stdout.splitlines()[0] if version_stdout.strip() else ""
    parts = first.split()
    if len(parts) < 3 or parts[0] != "ffmpeg" or parts[1] != "version":
        raise ParseError(f"not ffmpeg's -version output: {first!r}")
    return parts[2]


def filters_of(filters_stdout):
    """The filter names in -filters output, sorted: the score planner looks for the backend's."""
    names = set()
    for line in filters_stdout.splitlines():
        parts = line.split()
        if len(parts) >= 3 and "->" in parts[2] and parts[0] != "Filters:":
            names.add(parts[1])
    return sorted(names)


def ffvship_version_of(stdout):
    for token in stdout.split():
        if token.startswith("v") and token[1:2].isdigit():
            return token[1:]
    raise ParseError(f"not FFVship's --version output: {stdout.strip()[:80]!r}")


def sha256_file(path, buf=8 << 20):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while chunk := fh.read(buf):
            h.update(chunk)
    return h.hexdigest()


def tool_versions(ffmpeg):
    """(build, sha256 of the binary, its filter names), computed where the tool runs."""
    argv = as_cmd(ffmpeg)
    build = build_of(run([*argv, "-version"], timeout=60).stdout)
    filters = filters_of(run([*argv, "-hide_banner", "-filters"], timeout=60).stdout)
    binary = argv[-1] if len(argv) > 1 else argv[0]
    resolved = shutil.which(binary) or binary
    sha = sha256_file(resolved) if os.path.isfile(resolved) else "unknown"
    return build, sha, filters


def ffvship_version(ffvship):
    return ffvship_version_of(run([*as_cmd(ffvship), "--version"], timeout=60).stdout)


def free_bytes(path):
    usage = shutil.disk_usage(path)
    return usage.free
