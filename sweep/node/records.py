"""sweep/node/records.py -- a record is written once, atomically, beside the run's outputs; a crash between the write and
the post is recovered by re-posting what is on disk, and a differing rewrite is an error rather than a silent replacement."""
import json
import os
import pathlib


class RecordExists(Exception):
    pass


def write_record(directory, name, record):
    directory = pathlib.Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{name}.json"
    data = json.dumps(record, sort_keys=True, separators=(",", ":"))
    if path.exists():
        if path.read_text() == data:
            return path
        raise RecordExists(f"{path} holds a different record; a record is never overwritten")
    tmp = path.with_suffix(".tmp")
    tmp.write_text(data)
    os.replace(tmp, path)
    return path


def read_records(directory):
    directory = pathlib.Path(directory)
    if not directory.is_dir():
        return []
    return [json.loads(p.read_text()) for p in sorted(directory.glob("*.json"))]
