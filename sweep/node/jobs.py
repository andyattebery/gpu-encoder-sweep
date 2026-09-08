"""sweep/node/jobs.py -- what the agent does with one cell of the plan it was handed: encode it and count its frames,
probe a title, adopt a cut, score by recipe S1, time by recipe T1, publish a file with a sha on both ends. Every argv
comes from the plan or from the one builder with the plan's parameters; nothing here composes an encoder option."""
import dataclasses
import datetime as dt
import hashlib
import json
import os
import pathlib
import shutil
import time

from sweep import recipes
from sweep.hub import build
from sweep.node import ffm, pool


class JobError(Exception):
    def __init__(self, what, fix):
        super().__init__(f"{what} -- {fix}")
        self.what, self.fix = what, fix


def _now():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


class ReferenceCache:
    """One rescaled reference at a time, on disk, keyed by (path, geometry): every window's rescaled reference at once
    peaks around 13 GB for no benefit (sweep.py:1957). Cells are scored window by window, so a size of one hits."""

    def __init__(self, tmp):
        self.tmp, self.key, self.path, self.hits = pathlib.Path(tmp), None, None, 0

    def get(self, ref, w, h, score_ffmpeg):
        key = (ref, w, h)
        if key == self.key:
            self.hits += 1
            return str(self.path)
        self.release()
        self.tmp.mkdir(parents=True, exist_ok=True)
        dst = self.tmp / f"ref_{hashlib.sha256(ref.encode()).hexdigest()[:12]}_{w}x{h}.mkv"   # the identity is in the name
        _run_tool(score_ffmpeg, build.rescale_argv(ref, str(dst), w, h), "the reference rescale")
        self.key, self.path = key, dst
        return str(dst)

    def release(self):
        if self.path is not None:
            pathlib.Path(self.path).unlink(missing_ok=True)
        self.key, self.path = None, None


@dataclasses.dataclass
class Context:
    host: str
    os: str
    work_root: str
    share_root: str | None
    ffmpeg: list
    ffprobe: list
    ffvship: list | None
    score_ffmpeg: list | None
    run_dir: pathlib.Path
    refcache: ReferenceCache = dataclasses.field(init=False)

    def __post_init__(self):
        self.run_dir = pathlib.Path(self.run_dir)
        self.refcache = ReferenceCache(self.run_dir / "tmp")

    @property
    def progress_dir(self):
        return self.run_dir / "progress"

    @property
    def records_dir(self):
        return self.run_dir / "records"

    @property
    def tmp_dir(self):
        return self.run_dir / "tmp"


def _run_tool(tool, argv, what, timeout=None):
    proc = ffm.run([*tool, *argv], timeout=timeout)
    if proc.returncode != 0:
        raise JobError(f"{what} exited {proc.returncode}: {proc.stderr.strip()[-500:]}", "read its stderr; the tool did not do what was asked")
    return proc


def _decode_path(input_, ctx):
    """The plan carries a probe argv for a source cut; a lossless intermediate is decoded in software and carries none."""
    if input_.get("probe_argv") is None:
        return "software"
    try:
        return ffm.decode_probe(ctx.ffmpeg, input_["probe_argv"])
    except ffm.ToolError as e:
        raise JobError(str(e), "the probe must run for a decode path to be a measurement") from e


def _encode_once(cell, ctx, progress_name):
    """Run the cell's argv once; (frames, bytes on disk, out_time_s, wall_s) or a failure record."""
    ctx.progress_dir.mkdir(parents=True, exist_ok=True)
    pathlib.Path(cell["output"]).parent.mkdir(parents=True, exist_ok=True)
    progress = ctx.progress_dir / progress_name
    progress.unlink(missing_ok=True)
    started = time.perf_counter()
    proc = ffm.run([*ctx.ffmpeg, "-progress", str(progress), *cell["argv"]])
    wall = time.perf_counter() - started
    stderr = proc.stderr[-2000:]
    if proc.returncode != 0:
        return None, _failure(cell, stderr, proc.returncode)
    try:
        measured = ffm.parse_progress(progress.read_text() if progress.exists() else "")
    except ffm.ParseError as e:
        return None, _failure(cell, f"-progress: {e}\n{stderr}", proc.returncode)
    progress.unlink(missing_ok=True)
    if not os.path.isfile(cell["output"]):
        return None, _failure(cell, f"exited 0 and wrote no output at {cell['output']}\n{stderr}", proc.returncode)
    size = os.path.getsize(cell["output"])
    return {"frames": measured["frames"], "bytes": size, "out_time_s": measured["out_time_s"], "wall_s": round(wall, 3), "rc": proc.returncode, "stderr": stderr}, None


def _failure(cell, stderr, rc):
    return {"kind": "failure", "cell_key": cell["cell_key"], "at": _now(), "stderr": stderr or "(no stderr)", "rc": rc}


def encode_cell(cell, inputs, ctx):
    """An encode record -- frames from -progress, bytes from the disk (the -progress size is not the file's) -- or a
    failure record carrying the stderr; a leg short of its cut's frames is a failure, whatever the exit status."""
    input_ = inputs[(cell["window_id"], cell["cut_kind"])]
    decode_path = _decode_path(input_, ctx)
    measured, failure = _encode_once(cell, ctx, f"{cell['cell_key']}.progress")
    if failure is not None:
        return failure
    if measured["frames"] != input_["frames"]:
        pathlib.Path(cell["output"]).unlink(missing_ok=True)
        return _failure(cell, f"short of frames: {measured['frames']} of {input_['frames']}\n{measured['stderr']}", measured["rc"])
    if not cell.get("keep", False):
        pathlib.Path(cell["output"]).unlink(missing_ok=True)
    return {"kind": "encode", "cell_key": cell["cell_key"], "bytes": measured["bytes"],
            "bitrate_kbps": round(measured["bytes"] * 8 / 1000.0 / max(measured["out_time_s"], 1e-9), 2),
            "frames": measured["frames"], "duration_s": measured["out_time_s"], "decode_path": decode_path, "kept": bool(cell.get("keep", False))}


def time_cell(cell, inputs, ctx):
    """T1: the production argv repeated, the first sample flagged warm-up and kept, every leg verified by frame count;
    the encode is discarded. (An encode record with kept false, a timing record) or (a failure record,)."""
    input_ = inputs[(cell["window_id"], cell["cut_kind"])]
    decode_path = _decode_path(input_, ctx)
    samples, last = [], None
    for i in range(int(cell.get("repeats", 1))):
        measured, failure = _encode_once(cell, ctx, f"{cell['cell_key']}.{i}.progress")
        pathlib.Path(cell["output"]).unlink(missing_ok=True)
        if failure is not None:
            return (failure,)
        if measured["frames"] != input_["frames"]:
            return (_failure(cell, f"short of frames on repeat {i}: {measured['frames']} of {input_['frames']}\n{measured['stderr']}", measured["rc"]),)
        samples.append({"workers": 1, "repeat_index": i, "fps": round(measured["frames"] / max(measured["wall_s"], 1e-9), 2), "wall_s": measured["wall_s"],
                        "decode_path": decode_path, "is_warmup": i == 0, "noise_floor_pct": None, "leg": "full", "frames": measured["frames"]})
        last = measured
    encode = {"kind": "encode", "cell_key": cell["cell_key"], "bytes": last["bytes"],
              "bitrate_kbps": round(last["bytes"] * 8 / 1000.0 / max(last["out_time_s"], 1e-9), 2),
              "frames": last["frames"], "duration_s": last["out_time_s"], "decode_path": decode_path, "kept": False}
    return encode, {"kind": "timing", "cell_key": cell["cell_key"], "samples": samples}


# ---------------------------------------------------------------- the sample

SOURCE_TYPES = (("remux", ("REMUX",)), ("bluray", ("BluRay", "Blu-ray", "BDRip")), ("web", ("WEB-DL", "WEBRip", "WEB", "AMZN", "NF", "DSNP", "HMAX", "ATVP")))


def source_type_of(filename):
    """F1's filename heuristic: remux, bluray, web, else other -- recorded on the title and correctable by hand."""
    upper = filename.upper()
    for kind, marks in SOURCE_TYPES:
        if any(m.upper() in upper for m in marks):
            return kind
    return "other"


def _fps(rate):
    num, _, den = rate.partition("/")
    return float(num) / float(den or 1)


def inventory_title(spec, ctx):
    """A title record from ffprobe: the population's technical spread, the frame's fields (SPEC Stage 0, F1)."""
    proc = _run_tool(ctx.ffprobe, ["-v", "error", "-show_streams", "-show_format", "-of", "json", spec["path"]], f"ffprobe of {spec['path']}")
    probed = json.loads(proc.stdout)
    video = next((s for s in probed["streams"] if s.get("codec_type") == "video"), None)
    if video is None:
        raise JobError(f"{spec['path']} has no video stream", "inventory names video files")
    side = {d.get("side_data_type", ""): d for d in video.get("side_data_list", [])}
    dv = next((d for k, d in side.items() if "DOVI" in k.upper() or "DOLBY" in k.upper()), None)
    transfer = video.get("color_transfer", "")
    dynamic_range = "dv" if dv else "hdr10" if transfer == "smpte2084" else "hlg" if transfer == "arib-std-b67" else "sdr"
    fps = _fps(video.get("r_frame_rate", "24/1"))
    bit_depth = int(video["bits_per_raw_sample"]) if video.get("bits_per_raw_sample") else (10 if "10" in video.get("pix_fmt", "") else 8)
    bitrate = float(probed.get("format", {}).get("bit_rate") or 0) / 1000.0
    audio = [f"{s.get('codec_name', '?')} {s.get('channel_layout', s.get('channels', ''))}".strip() for s in probed["streams"] if s.get("codec_type") == "audio"]
    subs = [f"{s.get('codec_name', '?')} {s.get('tags', {}).get('language', '')}".strip() for s in probed["streams"] if s.get("codec_type") == "subtitle"]
    return {"kind": "title", "title_id": spec["title_id"], "path": spec["path"], "library": spec["library"], "width": int(video["width"]), "height": int(video["height"]),
            "dynamic_range": dynamic_range, "dv_profile": int(dv["dv_profile"]) if dv and dv.get("dv_profile") is not None else None,
            "video_codec": video.get("codec_name", "unknown"), "field_order": video.get("field_order", "progressive"), "fps": fps, "bit_depth": bit_depth,
            "bitrate_kbps": bitrate, "bpp": (bitrate * 1000.0) / (int(video["width"]) * int(video["height"]) * fps) if fps else 0.0,
            "source_type": source_type_of(os.path.basename(spec["path"])), "audio_layout": ", ".join(audio) or None,
            "subtitle_layout": ", ".join(subs) or None, "scanned_at": _now()}


def adopt_cut(cut, refset, ctx):
    """Register an existing cut: hash its decoded frames, count them, copy it into the reference set's home under the work
    root (a name already taken by a different file is refused, never replaced)."""
    src, dest = cut["path"], cut["dest"]
    if not os.path.isfile(src):
        raise JobError(f"{src} does not exist on {ctx.host}", "name the cut file as this host sees it")
    if os.path.isfile(dest):
        if os.path.getsize(dest) != os.path.getsize(src) or ffm.sha256_file(dest) != ffm.sha256_file(src):
            raise JobError(f"{dest} already holds a different file", "a cut is re-materialised only by content hash; remove the stale file by hand")
    else:
        pathlib.Path(dest).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dest)
    sha, frames = ffm.content_hash(ctx.ffmpeg, build.hash_argv(dest))
    chain = cut.get("chain") or {}
    return {"kind": "cut", "cut_id": cut["cut_id"], "reference_set_id": refset["id"], "window_id": cut["window_id"], "cut_kind": cut["kind"],
            "chain_lane": chain.get("lane"), "chain_host": chain.get("host"), "chain_unit": chain.get("encoder_unit_id"),
            "content_sha": sha, "bytes": os.path.getsize(dest), "frames": frames, "tags_pinned": None}


# ---------------------------------------------------------------- the exchange

def pull(spec, dest, ctx):
    """Copy a published file from the share to the work root and prove it by the sha the hub recorded."""
    if not ctx.share_root:
        raise JobError(f"{ctx.host} has no share root to pull {spec['relative']} from", "add-host with --share-root")
    src = pathlib.Path(ctx.share_root).joinpath(*spec["relative"].split("/"))
    if not src.is_file():
        raise JobError(f"{spec['relative']} is not on the share as {ctx.host} sees it", "publish it first")
    pathlib.Path(dest).parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, dest)
    sha = ffm.sha256_file(dest)
    if sha != spec["sha256"] or os.path.getsize(dest) != spec["bytes"]:
        pathlib.Path(dest).unlink(missing_ok=True)
        raise JobError(f"{spec['relative']} differs after the pull (sha {sha[:12]}, expected {spec['sha256'][:12]})", "the transfer is corrupt; pull again")
    return dest


def publish_file(spec, ctx):
    """Copy a file to the share and report the sha computed before the copy and after it; a share this runtime cannot
    write refuses before anything moves (the eta scoring container's CIFS mount is the case the probe exists for)."""
    if not ctx.share_root:
        raise JobError(f"{ctx.host} has no share root", "add-host with --share-root")
    share = pathlib.Path(ctx.share_root)
    probe = share / f".probe-{ctx.host}"
    try:
        probe.write_bytes(b"probe")
        probe.unlink()
    except OSError as e:
        raise JobError(f"the share is not writable from {ctx.host}: {e}", "the role mounts temp/harness read-write with the credential from the vault") from e
    before = ffm.sha256_file(spec["local"])
    dest = share.joinpath(*spec["relative"].split("/"))
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(spec["local"], dest)
    after = ffm.sha256_file(dest)
    if after != before:
        raise JobError(f"{spec['relative']} differs after the copy (sha {after[:12]} on the share, {before[:12]} before it)", "the transfer is corrupt; publish again")
    return {"path": spec["relative"], "by_host": ctx.host, "bytes": os.path.getsize(spec["local"]), "sha256": before,
            "run_id": spec.get("run_id"), "cell_key": spec.get("cell_key"), "cut_id": spec.get("cut_id")}


# ---------------------------------------------------------------- scoring

def _timed(fn):
    started = time.perf_counter()
    result = fn()
    return result, round(time.perf_counter() - started, 3)


def _ffvship(ctx, score, ref, enc, metric, out):
    _run_tool(score["tools"]["ffvship"], build.ffvship_argv(ref, enc, metric, str(out), score["tools"]["gpu_id"]), f"FFVship {metric}")
    if not out.is_file():
        raise JobError(f"FFVship {metric} exited 0 and wrote no JSON", "an absence is not a result; re-run the cell")
    rows = json.loads(out.read_text())
    out.unlink()
    return [[float(x) for x in (r if isinstance(r, list) else [r])] for r in rows]


def _libvmaf(ctx, score, ref, enc, out):
    cuda = score["metric_backend"] == "libvmaf_cuda"
    _run_tool(score["tools"]["score_ffmpeg"], build.libvmaf_argv(enc, ref, str(out), cuda, score.get("threads") or 0), "libvmaf")
    if not out.is_file():
        raise JobError("libvmaf exited 0 and wrote no log", "an absence is not a result; re-run the cell")
    pooled = json.loads(out.read_text()).get("pooled_metrics", {})
    out.unlink()
    values = {}
    for metric, names in (("vmaf", ("vmaf",)), ("cambi", ("cambi",)), ("psnr_y", ("psnr_y", "psnr")), ("float_ssim", ("float_ssim", "ssim"))):
        block = next((pooled[n] for n in names if isinstance(pooled.get(n), dict) and "mean" in pooled[n]), None)
        if block is None:
            raise JobError(f"libvmaf's log has no pooled {metric}; present: {sorted(pooled)}", "the filter string dropped a feature; the plan's argv names all four")
        values[metric] = float(block["mean"])
    return values


def score_cell(cell, score, ctx):
    """S1: both operands through the same software rescale (the reference once per window), FFVship twice and libvmaf once
    concurrently, the pooling by nearest rank; the encode is discarded unless the plan keeps it. Returns the score record
    and the per-frame arrays, which the agent uploads separately."""
    ctx.tmp_dir.mkdir(parents=True, exist_ok=True)
    enc = cell["encode"]["path"]
    if cell["encode"].get("pull"):
        pull(cell["encode"]["pull"], enc, ctx)
    ref = cell["reference"]["path"]
    if cell["reference"].get("pull"):
        pull(cell["reference"]["pull"], ref, ctx)
    w, h = score["w"], score["h"]
    ref_r, t_ref = _timed(lambda: ctx.refcache.get(ref, w, h, score["tools"]["score_ffmpeg"]))
    enc_r = ctx.tmp_dir / f"enc_{cell['cell_key']}_{w}x{h}.mkv"
    _, t_enc = _timed(lambda: _run_tool(score["tools"]["score_ffmpeg"], build.rescale_argv(enc, str(enc_r), w, h), "the encode rescale"))
    try:
        (ssimu2, t_s), (butter, t_b), (vmaf, t_v) = pool.run_all([
            lambda: _timed(lambda: _ffvship(ctx, score, ref_r, str(enc_r), "SSIMULACRA2", ctx.tmp_dir / f"{cell['cell_key']}.ssimu2.json")),
            lambda: _timed(lambda: _ffvship(ctx, score, ref_r, str(enc_r), "Butteraugli", ctx.tmp_dir / f"{cell['cell_key']}.butter.json")),
            lambda: _timed(lambda: _libvmaf(ctx, score, ref_r, str(enc_r), ctx.tmp_dir / f"{cell['cell_key']}.vmaf.json")),
        ], workers=3)
    finally:
        enc_r.unlink(missing_ok=True)
    s2 = [r[0] for r in ssimu2]
    infnorm = [r[-1] for r in butter]
    pooled = recipes.pool(s2)
    scores = [{"metric": "ssimulacra2", "statistic": "mean", "value": pooled["mean"]},
              {"metric": "ssimulacra2", "statistic": "p5", "value": pooled["p5"]},
              {"metric": "ssimulacra2", "statistic": "min", "value": pooled["min"]},
              {"metric": "butteraugli", "statistic": "max", "value": max(infnorm)}]
    scores += [{"metric": m, "statistic": "mean", "value": vmaf[m]} for m in ("vmaf", "cambi", "psnr_y", "float_ssim")]
    steps = [{"scoring_step": n, "seconds": s} for n, s in (("rescale_ref", t_ref), ("rescale_enc", t_enc), ("ssimu2", t_s), ("butteraugli", t_b), ("libvmaf", t_v))]
    keep = bool(score.get("keep", False))
    if not keep:
        pathlib.Path(enc).unlink(missing_ok=True)
    record = {"kind": "score", "cell_key": cell["cell_key"], "recipe": "S1", "kept": keep, "scores": scores, "steps": steps}
    return record, {"ssimulacra2": s2, "butteraugli": infnorm}
