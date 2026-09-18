"""sweep/hub/exchange.py -- the exchange: files that cross machines, held by the hub alone and proven by sha on both ends.

The exchange lives under the hub's SWEEP_SHARE (the pool's temp/harness): runs/<run_id>/enc/<cell_key>.mkv for encodes
bound for another machine, and refsets/<reference_set_id>/<window_id>.<kind>.mkv for reference sets. Nothing else
mounts it. An agent PUTs a file with the sha it computed before the send; the hub hashes the stream as it lands under a
temporary name, refuses a short or corrupt transfer, and only then moves the file into place and records the publish.
An agent GETs a published file and checks the recorded sha after the pull. Every host addresses its work root the same
way, so one relative path names a file everywhere; two runtimes on one machine skip the exchange through the viewer's
local_view, the owner's work root in the viewer's spelling. Stdlib only.
"""
import hashlib
import os
import pathlib
import re
import tempfile

from sweep.hub import store as st
from sweep.hub.refusals import Refusal

KINDS = {"enc": "runs/{run_id}/enc/{cell_key}.mkv", "cut": "refsets/{reference_set_id}/{window_id}.{cut_kind}.mkv"}
_SEGMENT = r"[^/]+"
_SHAPES = re.compile(rf"^(runs/{_SEGMENT}/enc/{_SEGMENT}\.mkv|refsets/{_SEGMENT}/{_SEGMENT}\.(reference|source)\.mkv)$")


def share_path(kind, **parts):
    """An exchange-relative path, forward slashes: enc(run_id, cell_key) or cut(reference_set_id, window_id, cut_kind)."""
    if kind not in KINDS:
        raise ValueError(f"share_path knows {sorted(KINDS)}, not {kind!r}")
    return KINDS[kind].format(**parts)


def check_path(relative):
    """The path names one of the two kinds and nothing outside them, or it is refused."""
    if not _SHAPES.match(relative or "") or ".." in relative.split("/"):
        raise Refusal(f"{relative} is not an exchange path",
                      "the exchange holds runs/<run_id>/enc/<cell_key>.mkv and refsets/<reference_set_id>/<window_id>.<kind>.mkv")


def _join(root, relative, os_name):
    parts = relative.split("/")
    if os_name == "windows":
        return str(pathlib.PureWindowsPath(root).joinpath(*parts))
    return str(pathlib.PurePosixPath(root).joinpath(*parts))


def work_path(host_row, relative):
    """The exchange layout under the host's work root: where a pull lands, and where a run's outputs live."""
    return _join(host_row["work_root"], relative, host_row["os"])


def viewed_path(viewer_row, owner_row, relative):
    """The owner's work-root file as the viewer sees it, on one machine, through the viewer's local_view."""
    if viewer_row["machine"] != owner_row["machine"]:
        raise Refusal(f"{viewer_row['host']} cannot see {owner_row['host']}'s work root: it is on machine {viewer_row['machine']}, "
                      f"{owner_row['host']} on {owner_row['machine']}", "publish and pull")
    if not viewer_row.get("local_view"):
        raise Refusal(f"{viewer_row['host']} has no local_view of {owner_row['host']}'s work root",
                      "add-host with --local-view, the owner's work root as this runtime sees it, or publish and pull")
    return _join(viewer_row["local_view"], relative, viewer_row["os"])


def sha256_file(path, buf=8 << 20):
    """sha256 of a whole file, streamed: the transfer's proof (tools/eta_relay.py:47 in the archive)."""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while chunk := fh.read(buf):
            h.update(chunk)
    return h.hexdigest()


def target_of(share_root, relative):
    return pathlib.Path(share_root).joinpath(*relative.split("/"))


class Receiver:
    """A stream landing under a temporary name beside its target, hashed as it goes; finish() proves it against what
    the agent declared, commit() moves it into place atomically, discard() leaves nothing. Two receivers of one path
    never share a temporary file."""

    def __init__(self, share_root, relative):
        self.relative, self.target = relative, target_of(share_root, relative)
        self.target.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix=f".{self.target.name}.", suffix=".part", dir=self.target.parent)
        self.temp, self._fh, self._hash, self.bytes = pathlib.Path(name), os.fdopen(fd, "wb"), hashlib.sha256(), 0

    def write(self, chunk):
        self._fh.write(chunk)
        self._hash.update(chunk)
        self.bytes += len(chunk)

    def finish(self, bytes_, sha256):
        """Close the stream and hold it against the declared size and sha; a disagreement discards it and refuses."""
        self._fh.close()
        if self.bytes != bytes_:
            self.discard()
            raise Refusal(f"{self.relative} is {self.bytes} bytes at the hub, {bytes_} before the send", "the transfer is short; publish again")
        actual = self._hash.hexdigest()
        if actual != sha256:
            self.discard()
            raise Refusal(f"{self.relative} differs in transit (sha {actual[:12]} at the hub, {sha256[:12]} before the send)",
                          "the transfer is corrupt; publish again")

    def commit(self):
        os.replace(self.temp, self.target)

    def discard(self):
        if not self._fh.closed:
            self._fh.close()
        self.temp.unlink(missing_ok=True)


def record_publish(conn, path, by_host, bytes_, sha256, published_at, run_id=None, cell_key=None, cut_id=None):
    """Write the publish once: an identical repost is a no-op, a differing one is refused; the DDL keeps it one product."""
    existing = conn.execute("SELECT run_id, cell_key, cut_id, by_host, bytes, sha256 FROM published WHERE path = ?", (path,)).fetchone()
    if existing is not None:
        if existing == (run_id, cell_key, cut_id, by_host, bytes_, sha256):
            return
        raise Refusal(f"the hub already holds a different {path}",
                      "a published file is never overwritten; publish under a new run, or remove it from the exchange by hand")
    st.insert(conn, "published", {"path": path, "run_id": run_id, "cell_key": cell_key, "cut_id": cut_id, "by_host": by_host,
                                  "bytes": bytes_, "sha256": sha256, "published_at": published_at})
