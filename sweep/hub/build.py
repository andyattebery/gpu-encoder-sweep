"""sweep/hub/build.py -- the one command builder: a cell's settings and the frontend's plumbing become argv at plan
time, in the hub, and the agent prepends the binary and composes nothing (id-scoping-reaches-every-builder).

The shapes are the archived harness's (sweep.py:690-874, :2344, :2454, :1879, :2117, :2266-2338, :5690), kept so the
acceptance run reproduces the committed bytes: the plumbing per frontend for a lossless intermediate input, the
hardware-decode flags for a production input, the software-decode bridge, the lossless rescale, the FFVship and
libvmaf invocations, and the legs as prefixes of the production argv. Encoder options come only from the settings the
cell carries (E1's order: mode selectors, the anchor, the rest by id); there is no hidden option here.
"""
from sweep.hub.refusals import Refusal

PLUMBING = ["-nostdin", "-y", "-hide_banner", "-loglevel", "error", "-nostats"]
FRONTENDS = ("nvenc", "vaapi", "qsv")
LIBVMAF_OPTS = "feature=name=cambi|name=psnr|name=float_ssim:log_fmt=json"
_KIND_ORDER = {"mode_selector": 0, "quality_anchor": 1}


def ordered_flags(conn, settings):
    """[(flag, value)] in E1's order from [(setting_id, value, role)]: mode selectors first, the anchor next, the rest by
    setting_id; every role is emitted (computed and default_resolved are real options the encoder saw)."""
    out = []
    for setting_id, value, _role in settings:
        row = conn.execute("SELECT flag, kind FROM setting WHERE setting_id = ?", (setting_id,)).fetchone()
        if row is None:
            raise Refusal(f"setting {setting_id!r} does not exist", "add it first with add-setting")
        out.append((_KIND_ORDER.get(row[1], 2), setting_id, row[0], str(value)))
    return [(flag, value) for _, _, flag, value in sorted(out)]


def hwaccel_flags(frontend, device):
    """The hardware-DECODE flags, one definition (sweep.py:2344). qsv takes -qsv_device: -vaapi_device is accepted and ignored there."""
    if frontend == "nvenc":
        return ["-hwaccel", "cuda", "-hwaccel_output_format", "cuda"]
    if frontend == "vaapi":
        return ["-hwaccel", "vaapi", "-hwaccel_output_format", "vaapi", "-vaapi_device", device]
    if frontend == "qsv":
        return ["-hwaccel", "qsv", "-hwaccel_output_format", "qsv", "-qsv_device", device]
    raise ValueError(f"unknown frontend {frontend!r}; one of {FRONTENDS}")


def encode_argv(frontend, codec, flags, device, src, dst, *, vf_template=None, software_decode=False):
    """The encode argv without the binary. A reference or source cut (no vf_template) is a lossless intermediate uploaded
    to the card; a production input (vf_template) is decoded in hardware and run through the chain, or, with the
    software bridge, decoded in software and uploaded before the chain."""
    if frontend not in FRONTENDS:
        raise ValueError(f"unknown frontend {frontend!r}; one of {FRONTENDS}")
    argv = list(PLUMBING)
    if vf_template is not None:
        if software_decode:
            argv += ["-vaapi_device", device] if frontend == "vaapi" else []
            argv += ["-i", src, "-vf", "format=p010,hwupload," + vf_template]
        else:
            argv += hwaccel_flags(frontend, device) + ["-i", src, "-vf", vf_template]
    elif frontend == "nvenc":
        argv += ["-i", src]
    elif frontend == "vaapi":
        argv += ["-vaapi_device", device, "-i", src, "-vf", "format=p010,hwupload"]
    else:   # qsv: a fixed-size pool, so extra frames or the upload stalls (sweep.py:782)
        argv += ["-init_hw_device", f"qsv=hw,child_device={device}", "-filter_hw_device", "hw", "-i", src,
                 "-vf", "format=p010,hwupload=extra_hw_frames=64"]
    argv += ["-c:v", f"{codec}_{frontend}"]
    for flag, value in flags:
        argv += [flag, value]
    if frontend == "nvenc" and vf_template is None:
        argv += ["-pix_fmt", "p010le"]
    if codec == "hevc":
        argv += ["-tag:v", "hvc1"]
    return argv + ["-an", "-sn", dst]


def probe_argv(frontend, device, src):
    """One frame through the hardware decoder, stderr verbose: the decode path is read from its signatures (sweep.py:2377)."""
    return ["-nostdin", "-hide_banner", "-loglevel", "verbose", "-nostats", *hwaccel_flags(frontend, device),
            "-i", src, "-frames:v", "1", "-f", "null", "-"]


def hash_argv(src):
    """framemd5 of the decoded frames: the content hash, never the container's bytes (sweep.py:3838)."""
    return ["-nostdin", "-hide_banner", "-loglevel", "error", "-i", src, "-f", "framemd5", "-"]


def rescale_argv(src, dst, w, h):
    """S1 step 1: the software scaler, lossless ffvhuff, the two speed levers measured framemd5-identical (sweep.py:1879)."""
    return ["-nostdin", "-y", "-v", "error", "-max_frame_delay", "16", "-i", src, "-vf", f"scale=w={w}:h={h}",
            "-c:v", "ffvhuff", "-pred", "plane", "-pix_fmt", "yuv420p10le", dst]


def ffvship_argv(ref, enc, metric, out_json, gpu_id):
    """S1 step 2: --source, not --reference (the binary rejects the documented spelling); threads at their default (sweep.py:2117)."""
    argv = ["--source", ref, "--encoded", enc, "-m", metric, "--json", out_json]
    return argv + (["--gpu-id", str(gpu_id)] if gpu_id else [])


def libvmaf_graph(cuda):
    if not cuda:
        return "[0:v][1:v]libvmaf="
    return "[0:v]format=yuv420p10le,hwupload_cuda[d];[1:v]format=yuv420p10le,hwupload_cuda[r];[d][r]libvmaf_cuda="


def libvmaf_argv(enc, ref, out_json, cuda, threads):
    """S1 step 3: input 0 is the DISTORTED encode, input 1 the reference; 10-bit in; the device before the inputs (sweep.py:2286)."""
    log_path = str(out_json).replace("\\", "/").replace("'", r"\'")
    opts = f"{LIBVMAF_OPTS}:log_path='{log_path}'" + (f":n_threads={threads}" if threads else "")
    return [*PLUMBING, *(["-init_hw_device", "cuda=cu", "-filter_hw_device", "cu"] if cuda else []),
            "-i", enc, "-i", ref, "-lavfi", libvmaf_graph(cuda) + opts, "-f", "null", "-"]


def leg(argv, name):
    """A timing leg is a prefix of the production argv: decode stops before the filters, decode_filters before the
    encoder, full is the whole thing (sweep.py:5690). Marginal costs from an ablation, never a serial decomposition."""
    if name == "full":
        return list(argv)
    if name not in ("decode", "decode_filters"):
        raise ValueError(f"unknown leg {name!r}; one of full, decode, decode_filters")
    if "-c:v" not in argv:
        raise ValueError("a production argv has -c:v")
    cut = argv.index("-c:v")
    if name == "decode" and "-vf" in argv:
        cut = min(cut, argv.index("-vf"))
    return argv[:cut] + ["-f", "null", "-"]
