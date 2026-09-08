"""sweep/hub/exchange.py -- the share as the data plane, spelled per host, and a transfer proven by sha on both ends.

The share lives under temp/harness/: runs/<run_id>/enc/<cell_key>.mkv for encodes bound for another machine, and
refsets/<reference_set_id>/<window_id>.<kind>.mkv for reference sets. Each host addresses it from its own share_root,
and its work root the same way, so one relative path names a file everywhere. Two runtimes on one machine skip the
share: the viewer's local_view is the owner's work root in the viewer's spelling. A publish copies a file to the
share and posts the sha computed before the copy; the hub hashes the file at its bind-mounted pool path and records
the publish only when the two agree. Stdlib only.
"""
import hashlib
import pathlib

from sweep.hub import store as st
from sweep.hub.refusals import Refusal

KINDS = {"enc": "runs/{run_id}/enc/{cell_key}.mkv", "cut": "refsets/{reference_set_id}/{window_id}.{cut_kind}.mkv"}


def share_path(kind, **parts):
    """A share-relative path, forward slashes: enc(run_id, cell_key) or cut(reference_set_id, window_id, cut_kind)."""
    if kind not in KINDS:
        raise ValueError(f"share_path knows {sorted(KINDS)}, not {kind!r}")
    return KINDS[kind].format(**parts)


def _join(root, relative, os_name):
    parts = relative.split("/")
    if os_name == "windows":
        return str(pathlib.PureWindowsPath(root).joinpath(*parts))
    return str(pathlib.PurePosixPath(root).joinpath(*parts))


def spell(host_row, relative):
    """The share path in the host's spelling."""
    return _join(host_row["share_root"], relative, host_row["os"])


def work_path(host_row, relative):
    """The same layout under the host's work root: where a pull lands, and where a run's outputs live."""
    return _join(host_row["work_root"], relative, host_row["os"])


def viewed_path(viewer_row, owner_row, relative):
    """The owner's work-root file as the viewer sees it, on one machine, through the viewer's local_view."""
    if viewer_row["machine"] != owner_row["machine"]:
        raise Refusal(f"{viewer_row['host']} cannot see {owner_row['host']}'s work root: it is on machine {viewer_row['machine']}, "
                      f"{owner_row['host']} on {owner_row['machine']}", "publish to the share and pull")
    if not viewer_row.get("local_view"):
        raise Refusal(f"{viewer_row['host']} has no local_view of {owner_row['host']}'s work root",
                      "add-host with --local-view, the owner's work root as this runtime sees it, or publish to the share and pull")
    return _join(viewer_row["local_view"], relative, viewer_row["os"])


def sha256_file(path, buf=8 << 20):
    """sha256 of a whole file, streamed: the transfer's proof (tools/eta_relay.py:47 in the archive)."""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while chunk := fh.read(buf):
            h.update(chunk)
    return h.hexdigest()


def verify_arrival(share_root, relative, bytes_, sha256):
    """The file at the hub's pool path has the size and the sha the agent reported before the copy, or the transfer is refused."""
    path = pathlib.Path(share_root).joinpath(*relative.split("/"))
    if not path.is_file():
        raise Refusal(f"{relative} is not on the share", "the agent's copy did not arrive; publish again")
    size = path.stat().st_size
    if size != bytes_:
        raise Refusal(f"{relative} is {size} bytes on the share, {bytes_} before the copy", "the transfer is short; publish again")
    actual = sha256_file(path)
    if actual != sha256:
        raise Refusal(f"{relative} differs after the copy (sha {actual[:12]} on the share, {sha256[:12]} before it)",
                      "the transfer is corrupt; publish again")


def record_publish(conn, path, by_host, bytes_, sha256, published_at, run_id=None, cell_key=None, cut_id=None):
    """Write the publish once: an identical repost is a no-op, a differing one is refused; the DDL keeps it one product."""
    existing = conn.execute("SELECT run_id, cell_key, cut_id, by_host, bytes, sha256 FROM published WHERE path = ?", (path,)).fetchone()
    if existing is not None:
        if existing == (run_id, cell_key, cut_id, by_host, bytes_, sha256):
            return
        raise Refusal(f"the share already holds a different {path}",
                      "a published file is never overwritten; publish under a new run, or remove it from the share by hand")
    st.insert(conn, "published", {"path": path, "run_id": run_id, "cell_key": cell_key, "cut_id": cut_id, "by_host": by_host,
                                  "bytes": bytes_, "sha256": sha256, "published_at": published_at})
