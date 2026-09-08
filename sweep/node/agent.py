"""sweep/node/agent.py -- the agent: long-poll a claim, run the cells the plan hands it that have no record yet, post
each record as it completes, heartbeat as it goes, read the abandon flag between cells, ack at the end. It composes no
flags: every argv is the plan's or the one builder's with the plan's parameters. Three environment variables configure
it; the rest comes from the hub. `sweep-node serve | identify | hash PATH...`.

    SWEEP_HUB=https://hub SWEEP_TOKEN=... SWEEP_HOST=media-01 sweep-node serve
"""
import argparse
import json
import os
import pathlib
import sys
import threading
import time

import httpx

from sweep.hub import build
from sweep.node import ffm, identity as node_identity, jobs, records
from sweep.node.config import Config

ENCODING_STAGES = frozenset({"screen", "locate", "encode", "time", "split", "concurrency", "viewing", "probe", "calibrate"})


class Killed(Exception):
    """A test knob: the agent stops here as if its process had died."""


class Agent:
    def __init__(self, config, client, clock=time.monotonic):
        self.config, self.client, self.clock = config, client, clock
        self.host_row = self.scorer_row = self.identity = None
        self.heartbeat_s, self.ttl_s = 30, 90
        self.log, self.cells_run, self.reposted = [], 0, 0
        self.before_cell = None          # a test hook: called with the cell's index before it runs
        self._current = (None, 0, 0)     # (run_id, done, total) for the heartbeat thread

    # ---- talking to the hub
    def _say(self, text):
        self.log.append(text)
        print(f"sweep-node[{self.config.host}]: {text}", flush=True)

    def _post(self, path, body):
        return self.client.post(path, json=body)

    def start(self):
        r = self.client.get(f"/agents/{self.config.host}/config")
        if r.status_code != 200:
            raise SystemExit(r.text)
        cfg = r.json()
        self.host_row, self.scorer_row = cfg["host"], cfg["scorer"]
        self.heartbeat_s, self.ttl_s = cfg["heartbeat_s"], cfg["ttl_s"]
        self.identity = node_identity.identify(self.config, self.host_row, self.scorer_row)
        self.heartbeat()

    def heartbeat(self, run_id=None, done=0, total=0):
        body = {"run_id": run_id, "cells_done": done, "cells_total": total, "artifact": self.identity["artifact"], "identity": self.identity}
        r = self._post(f"/agents/{self.config.host}/heartbeat", body)
        if r.status_code != 200:
            self._say(f"heartbeat refused: {r.text}")
            return None
        return r.json().get("run_state")

    def claim(self, block_s=0):
        r = self._post(f"/agents/{self.config.host}/claim", {"block_s": block_s})
        if r.status_code == 204:
            return None
        if r.status_code != 200:
            self._say(f"claim: {r.text}")
            return None
        return r.json()

    def post_event(self, run_id, state, detail=None):
        r = self._post(f"/runs/{run_id}/events", {"state": state, "detail": detail})
        if r.status_code != 200:
            self._say(f"event {state} refused: {r.text}")

    def post_records(self, run_id, batch):
        if not batch:
            return True
        r = self._post(f"/runs/{run_id}/records", {"records": batch})
        if r.status_code != 200:
            self._say(f"records refused for {', '.join(str(b.get('cell_key') or b.get('title_id') or b.get('cut_id') or b.get('reference_set_id')) for b in batch)}: {r.text}")
            return False
        return True

    def abandoned(self, run_id):
        r = self.client.get(f"/runs/{run_id}/abandon")
        return r.status_code == 200 and r.json().get("abandon", False)

    # ---- the loop
    def serve_once(self, block_s=0, die_after=None, before_post=False):
        """One claim: a run or a publish job, run to its ack; None when nothing was waiting. The two knobs are the tests'."""
        claim = self.claim(block_s)
        if claim is None:
            return None
        if claim.get("kind") == "publish":
            self.run_publish(claim)
            return f"publish:{claim['entry_id']}"
        try:
            self.run_job(claim, die_after=die_after, before_post=before_post)
        except Killed:
            return claim["run"]["run_id"]
        return claim["run"]["run_id"]

    def serve(self):
        self.start()
        stop = threading.Event()
        threading.Thread(target=self._beat_loop, args=(stop,), daemon=True).start()
        try:
            while True:
                if self.serve_once(block_s=30) is None:
                    time.sleep(1)
        finally:
            stop.set()

    def _beat_loop(self, stop):
        while not stop.is_set():
            try:
                self.heartbeat(*self._current)
            except Exception as e:      # noqa: BLE001 -- the beat outlives any one failure
                self._say(f"heartbeat failed: {e}")
            stop.wait(self.heartbeat_s)

    def context(self, run_id):
        h = self.host_row
        ffmpeg = ffm.as_cmd(h["ffmpeg"]) if h.get("ffmpeg") else list(self.scorer_row["score_ffmpeg"])
        probe = list(ffmpeg)
        probe[-1] = str(pathlib.PurePath(probe[-1]).with_name(pathlib.PurePath(probe[-1]).name.replace("ffmpeg", "ffprobe")))
        return jobs.Context(host=h["host"], os=h["os"], work_root=h["work_root"], share_root=h.get("share_root"), ffmpeg=ffmpeg, ffprobe=probe,
                            ffvship=self.scorer_row["ffvship"] if self.scorer_row else None,
                            score_ffmpeg=self.scorer_row["score_ffmpeg"] if self.scorer_row else None,
                            run_dir=pathlib.Path(h["work_root"]) / "runs" / run_id)

    def _repost(self, run_id, ctx, done):
        """Records on disk the hub does not hold yet: a crash between the write and the post is recovered here."""
        held = [r for r in records.read_records(ctx.records_dir) if _key_of(r) not in done]
        if held:
            batch = [{k: v for k, v in r.items() if not k.startswith("_")} for r in held]
            if self.post_records(run_id, batch):
                self.reposted += len(batch)
                for r in held:
                    if r.get("kind") == "score" and "_frames" in r:
                        self._upload_frames(run_id, r["cell_key"], r["_frames"])
                    done.add(_key_of(r))

    def _upload_frames(self, run_id, cell_key, frames):
        for metric, values in frames.items():
            r = self._post(f"/runs/{run_id}/frames", {"cell_key": cell_key, "metric": metric, "values": values})
            if r.status_code != 200:
                self._say(f"frames refused for {cell_key} {metric}: {r.text}")

    def run_job(self, claim, die_after=None, before_post=False):
        run = claim["run"]
        run_id, stage = run["run_id"], run["stage"]
        ctx = self.context(run_id)
        done = set(claim.get("done", []))
        self._repost(run_id, ctx, done)
        units = self._units(claim, stage)
        total = len(units)
        self._current = (run_id, len(done), total)
        self.post_event(run_id, "running", "first cell started")
        self.heartbeat(run_id, len(done), total)
        inputs = {(i["window_id"], i["kind"]): i for i in claim.get("inputs", []) if "window_id" in i}
        posted = 0
        for n, (key, unit) in enumerate(units):
            if key in done:
                continue
            if self.before_cell is not None:
                self.before_cell(n)
            if n > 0 and self.abandoned(run_id):
                self._say(f"{run_id} abandoned; stopping before cell {n}")
                break
            try:
                produced = self._run_unit(stage, unit, inputs, claim, ctx)
            except jobs.JobError as e:
                self._say(f"{key}: {e}")
                continue
            self.cells_run += 1
            batch = []
            for name, record in produced:
                stored = dict(record)
                try:
                    records.write_record(ctx.records_dir, name, stored)
                except records.RecordExists as e:
                    self._say(f"{key}: {e}")
                    continue
                batch.append({k: v for k, v in record.items() if not k.startswith("_")})
            if die_after is not None and before_post and posted + 1 >= die_after:
                raise Killed()
            if self.post_records(run_id, batch):
                done.add(key)
                for name, record in produced:
                    if record.get("kind") == "score":
                        self._upload_frames(run_id, record["cell_key"], record["_frames"])
            posted += 1
            self._current = (run_id, len(done), total)
            self.heartbeat(run_id, len(done), total)
            if die_after is not None and posted >= die_after:
                raise Killed()
        r = self._post(f"/runs/{run_id}/ack", {"entry_id": claim["entry_id"]})
        self._say(f"{run_id}: {r.text}")
        self._current = (None, 0, 0)
        self.heartbeat()

    def _units(self, claim, stage):
        """(key, what to run) per unit of work, in the plan's order."""
        if stage == "inventory":
            return [(i["title_id"], i) for i in claim["inputs"]]
        if stage == "materialise":
            return [(c["cut_id"], c) for c in claim["cuts"]]
        return [(c["cell_key"], c) for c in claim["cells"]]

    def _run_unit(self, stage, unit, inputs, claim, ctx):
        """[(local name, record)] for one unit of work."""
        if stage == "inventory":
            return [(unit["title_id"], jobs.inventory_title(unit, ctx))]
        if stage == "materialise":
            out = []
            refset = claim["reference_set"]
            if refset.get("post") and not (ctx.records_dir / "reference_set.json").exists():
                out.append(("reference_set", {"kind": "reference_set", "reference_set_id": refset["id"], "geometry": refset["geometry"], "pix_fmt": refset["pix_fmt"],
                                              "built_with": f"{self.identity['ffmpeg_build']} {self.identity['ffmpeg_sha']}", "built_at": jobs._now()}))
            out.append((unit["cut_id"], jobs.adopt_cut(unit, refset, ctx)))
            return out
        if stage == "score":
            record, frames = jobs.score_cell(unit, claim["score"], ctx)
            return [(unit["cell_key"], dict(record, _frames=frames))]
        if stage in ("time", "split", "concurrency"):
            produced = jobs.time_cell(unit, inputs, ctx)
            if len(produced) == 1:
                return [(unit["cell_key"], produced[0])]
            return [(f"{unit['cell_key']}.encode", produced[0]), (f"{unit['cell_key']}.timing", produced[1])]
        return [(unit["cell_key"], jobs.encode_cell(unit, inputs, ctx))]

    def run_publish(self, claim):
        ctx = self.context("publish")
        done = set(claim.get("done", []))
        for spec in claim["files"]:
            if spec["relative"] in done:
                continue
            try:
                body = jobs.publish_file(spec, ctx)
            except jobs.JobError as e:
                self._say(f"publish {spec['relative']}: {e}")
                continue
            r = self._post("/exchange/published", body)
            if r.status_code != 200:
                self._say(f"published refused for {spec['relative']}: {r.text}")
        r = self._post(f"/agents/{self.config.host}/ack", {"entry_id": claim["entry_id"]})
        self._say(f"publish job {claim['entry_id']}: {r.text}")


def _key_of(record):
    return record.get("cell_key") or record.get("title_id") or record.get("cut_id") or record.get("reference_set_id")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="sweep-node", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="verb", required=True)
    sub.add_parser("serve", help="claim and run, forever")
    sub.add_parser("identify", help="print what this agent would report")
    h = sub.add_parser("hash", help="the content hash (decoded frames) and frame count of each file")
    h.add_argument("paths", nargs="+")
    args = ap.parse_args(argv)
    config = Config.from_env(os.environ)
    client = httpx.Client(base_url=config.hub, headers={"Authorization": f"Bearer {config.token}"}, timeout=None)
    agent = Agent(config, client)
    if args.verb == "serve":
        agent.serve()
        return 0
    r = client.get(f"/agents/{config.host}/config")
    if r.status_code != 200:
        print(r.text)
        return 1
    cfg = r.json()
    if args.verb == "identify":
        print(json.dumps(node_identity.identify(config, cfg["host"], cfg["scorer"]), indent=2, sort_keys=True))
        return 0
    ffmpeg = ffm.as_cmd(cfg["host"]["ffmpeg"]) if cfg["host"].get("ffmpeg") else cfg["scorer"]["score_ffmpeg"]
    for path in args.paths:
        sha, frames = ffm.content_hash(ffmpeg, build.hash_argv(path))
        print(f"{sha}  {frames}  {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
