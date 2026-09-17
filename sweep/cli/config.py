"""sweep/cli/config.py -- where the hub and the token are kept, so a fresh shell needs no exports.

One file of `KEY=value` lines, `0600`, holding `SWEEP_HUB` and `SWEEP_TOKEN` and nothing else. The CLI reads it when
the environment does not carry a value; `sweep config --save` writes it. A file anyone but the owner can read is
refused rather than used -- it holds a bearer token, and a warning nobody reads is not a guard. Stdlib only.
"""
import os
import pathlib

from sweep import model_check as mc

KEYS = ("SWEEP_HUB", "SWEEP_TOKEN")


def path(env):
    """$SWEEP_CONFIG, else $XDG_CONFIG_HOME/sweep/env, else ~/.config/sweep/env."""
    if env.get("SWEEP_CONFIG"):
        return pathlib.Path(env["SWEEP_CONFIG"])
    base = env.get("XDG_CONFIG_HOME") or os.path.join(env.get("HOME") or pathlib.Path.home(), ".config")
    return pathlib.Path(base) / "sweep" / "env"


def read(env):
    """{key: value} from the file, or {} when there is none. A file named by $SWEEP_CONFIG must exist: naming one
    and having it absent is a mistake worth hearing about, which is the rule SWEEP_AGENT_TOKENS already follows."""
    p = path(env)
    if not p.is_file():
        if env.get("SWEEP_CONFIG"):
            raise SystemExit(mc.refusing(f"SWEEP_CONFIG names {p}, which does not exist",
                                         f"write it as {KEYS[0]}=… and {KEYS[1]}=… at mode 600, or unset SWEEP_CONFIG"))
        return {}
    if os.stat(p).st_mode & 0o077:
        raise SystemExit(mc.refusing(f"{p} holds a token and is readable by more than its owner",
                                     f"chmod 600 {p}"))
    out = {}
    for n, line in enumerate(p.read_text().splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        line = line.removeprefix("export ").lstrip()
        key, sep, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if not sep or key not in KEYS:
            raise SystemExit(mc.refusing(f"{p} line {n} is {line!r}, which names no setting this reads",
                                         f"the file holds {' and '.join(KEYS)}, one KEY=value per line"))
        if len(value) > 1 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        out[key] = value
    return out


def write(env, values):
    """Write the given keys to the file at 0600, in a 0700 directory; returns the path. Opened with the mode and
    chmoded after, so a file that already existed is tightened rather than left as it was."""
    p = path(env)
    p.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    body = "".join(f"{k}={values[k]}\n" for k in KEYS if values.get(k))
    with os.fdopen(os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:
        f.write(f"# written by `sweep config --save`; read when SWEEP_HUB and SWEEP_TOKEN are not in the environment\n{body}")
    os.chmod(p, 0o600)
    return p
