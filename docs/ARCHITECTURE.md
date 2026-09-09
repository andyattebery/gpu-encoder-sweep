# Architecture

**What this is.** How the harness is built: the shape, the record, the API, the queue and the agent
protocol, the exchange, the node images, deployment, the module map, and how each refusal that the
spec marks `by_construction` is closed. The process it implements is `SPEC.md`; the data model is
`DATA-MODEL.md`, rendered from `sweep/schema.sql`; the invariants are `refusals.json`. No status, no
dates, no history live here.

### The shape

```
laptop                       nas-01 (always on)                       GHCR                nodes
  sweep CLI ── REST/TLS ──►  hub: FastAPI + SQLite (the record)        hub image            media-01: encode + score containers
  (thin: calls, prints)        checks on every write · planner ·       node-encode          htpc-01:  encode container (podman)
  campaign repo ◄─ export ─   builder · ingest · analysis · render     node-encode-mesarc   eta:      native encode agent (pyz)
  record/                      Redis behind it: queue · heartbeats     node-score           eta-wsl:  score container
                               share (pool path, bind-mounted):        CI builds and         agents: long-poll /claim, pull inputs
                               temp/harness/{runs,refsets}             pushes on tags        from the share, post records + events
```

- **The hub's database is the record.** Every write goes through the API, and the API refuses a
  write that would make a check fire — the same `x_*` views and script checks `model_check.py`
  proves today, run inside the hub in a transaction. `sweep export` pulls authored intent, plans,
  events, records and calibrated constants into `record/` in the campaign repo as deterministic JSON,
  so git history, review by diff and the acceptance comparison stay possible; the SQLite file lives in the
  compose data directory the backup role already covers.
- **One command builder, in the hub.** Settings + host + `host_unit.device` + frontend become argv
  at plan time; an agent executes what it is given and composes no flags. Verdicts (V1), N\* (T1),
  BD-rate (R1), inversion and categorisation are computed in the hub over the store, never on a node.
- **Scoring is a run.** `run.stage` gains `score`; a score run has `parent_run_id` and no cells of
  its own, so a 10-hour job has the same state machine as an encode. Its `score` and `step_trace`
  rows name it, so two scorers with one build never collide.
- **A host is a runtime; a machine may hold several.** `host.machine` groups them and the quiet-box
  rule is per machine: `media-01` (encode container, both units) and `media-01-score`; `htpc-01`;
  `eta` (native) and `eta-wsl` (score container). `host.local_view` says how one runtime sees
  another's work root on the same machine, so eta's own cells are scored through `/mnt/d` with no
  copy.
- **A scorer is a catalogue row**: `scorer(host, ffvship, score_ffmpeg, metric_backend, gpu_id,
  cache_dir)`, intent only; the build it ran is observed per run. media-01-score and eta-wsl run the
  same node-score image — FFVship 5.1.0 CUDA and `libvmaf_cuda` from the linux64 jellyfin-ffmpeg
  build — so their `scorer_build` is identical and only the GPU differs, which is why a score row
  names its run.

### The images and the artifact

| image | base | adds | runs on |
|---|---|---|---|
| `ghcr.io/andyattebery/gpu-encoder-sweep-hub` | `python:<pinned>-slim` | the package with its `hub` extra (`fastapi`, `uvicorn`, `redis`), installed by uv from the lock file | nas-01 |
| `…-node-encode` | `ghcr.io/haveagitgat/tdarr_node:<the tag the tdarr role pins>` — production's image, so the VAAPI driver and Mesa are production's | python3; the agent; the linux64 portable jellyfin-ffmpeg from `github.com/andyattebery/jellyfin-ffmpeg` releases at a pinned tag | media-01 |
| `…-node-encode-mesarc` | `ghcr.io/andyattebery/tdarr-node-mesa-fresh:mesarc` by digest — the same tdarr layer as `node-encode` (it shares all 15 of its parent's layers) with Mesa from `ppa:ernstp/mesarc`, which is the VAAPI driver htpc-01 encodes with | everything `node-encode` adds, plus a build-time assertion that the apt layer left `mesa-libgallium` and `radeonsi_drv_video.so` alone | htpc-01 |
| `…-node-score` | `nvidia/cuda:13.3.1-runtime-ubuntu26.04` (FFVship needs libavutil ≥ 7, per `ffvship/Dockerfile.cuda`) | FFVship and `libvship.so` from a build stage at Vship v5.1.0 (from Codeberg, the source; GitHub is a stale mirror) — **the version the committed values were scored with, pinned on purpose**: a newer tag is a new `scorer_build`, which the acceptance comparison cannot be run across; the same jellyfin-ffmpeg build (`libvmaf_cuda`); python3; the agent | media-01, eta-wsl |

The native Windows agent is the same package, run by the boot task as `uvx --from
"gpu-encoder-sweep[node] @ git+https://github.com/andyattebery/gpu-encoder-sweep@<sha or tag>" sweep-node serve`;
uv resolves the dependencies and the interpreter, and updating it is the role bumping the ref. The
package versions itself from git (hatch-vcs: `X.Y.Z` at a tag, `X.Y.Z.devN+g<sha>` past one), and an
artifact is `<flavour>:<version>` — `node-encode:…`, `node-encode-mesarc:…`, `node-score:…`, `hub:…`
from the stamp CI writes into the image, `uvx:…` on eta — with `harness_version` the git sha (or the
tag, which names one). An agent reports its identity — the artifact, the ffmpeg build, sha and
filters, FFVship's version — in every heartbeat, and the hub keeps each change as a `host_identity`
row. A plan pins the identity its host last reported, and the claim hands the run over only while
the host still reports that artifact (a 409 with the fix otherwise), so a node running code the plan
was not built for is refused rather than silently measured. The infrastructure repo pins the GHCR
tags and the git ref its stacks and tasks run.

### Schema changes the architecture requires

Landed in M1, and the three rows marked M2 in M2 (the legacy row is M3's), each with fixture rows, a mutation case and a DDL
refusal.

| change | why |
|---|---|
| `chain` keyed `(lane, host, encoder_unit_id)`; `cut.chain_unit`; `x_shipped_without_chain` joins on the unit | media-01 holds two units of different frontends and the filter graph is per API |
| `run.stage` + `score`; `run.parent_run_id`; `x_score_run_without_parent`; a score run's `run_window` copies its parent's; `score.run_id` and `step_trace.run_id` in the primary key, so `v_run_progress` counts a score run's own rows against its parent's cells and two scorers with one build never collide; `x_run_unit_not_on_host` exempts score runs | scoring gets run state, and a score row names the run that made it |
| `x_two_active_runs_on_a_host` → `x_timing_run_not_alone`, per `host.machine`: a `time`/`concurrency`/`split` run active on a machine with any other active run; enforced at enqueue and again at claim, so a queued timing run is not handed out until its machine is quiet | quiet box for timing only |
| `x_supported_lane_not_routed` scoped to lanes with at least one `shipped` row; `render --check` keeps the completeness rule over the whole case set | it fired on every store before Stage 11 ever ran |
| the mandatory path skips a codec-ladder rung outside the anchor's declared range (`setting.range_lo`/`range_hi`, the frontend's limit) and reports it UNREACHABLE; the store refuses a cell past it (`x_cell_anchor_outside_range`) and `shipping_arm_ladder_complete` requires in-range rungs only | the AV1 ladder runs to 60 and `av1_qsv` ends at 51 |
| `scorer` (reference, FILE): host, `ffvship` argv, `score_ffmpeg` argv, `metric_backend ∈ {libvmaf, libvmaf_cuda}`, `gpu_id`, `cache_dir` — intent only, nothing observed; a score run's host must have a scorer row, and the build it ran is `run.scorer_build`, from the artifact the agent reports | scoring on more than one host, without a FILE row that goes stale on an image bump |
| `scorer_equivalence` (measurement, ROW, `@writer equivalence`): run_a, run_b, metric, statistic, cells, max_abs_delta, `exact`, computed over the two score runs' own rows; `x_search_mixed_scorers_without_equivalence`: a search whose score rows come from two scorers (score hosts) with no equivalence between them marked `exact` for `ssimulacra2` and `butteraugli` — keyed on the host, not the build, because one image gives both scorers the same build and only the GPU differs | parallel scoring across scorers only where the instruments are proven the same |
| `host.machine`, `host.local_view`, `host.share_root`; a `nas-01` host row with no units; `run.artifact`; `run_event.by ∈ {hub, agent}` | runtimes on one machine; the share per host; a plan and an agent agree on the code that ran; events say who wrote them |
| `run.stage` + `materialise`, `verify`, `inventory`; `run.encoder_unit_id` NULL only for `verify` and `inventory` (a CHECK ties it to the stage); `x_run_unit_not_on_host` exempts them | materialise runs the chain on a node's GPU, verify hashes frames on every host, inventory scans the library where it is mounted: all three are runs the queue hands out, and none is a search |
| `admissibility_verdict` (measurement, ROW, `@writer screen`): unit, `test ∈ {opens, monotone, obeys_rate}`, window, base setting and value, verdict, reason, its cells; `x_search_mode_not_admissible`: a search whose anchor's mode lacks an admissible `opens` and `monotone` verdict on its unit | Stage 1's tests 1, 3 and 6 are refusals the store holds, and Stages 2 and 10 refuse against them |
| `timing.frames` NOT NULL; `x_timing_short_of_frames` against the cut | a null-terminated leg processes zero frames and exits 0; a leg is verified by frame count, per leg |
| `encode.kept` is flipped by ingest on the score record — the one column with a second writer, and the schema names it | scoring discards the encode; the fact is written where the discard is decided |
| five checks that are preconditions of a later stage are scoped to it: `x_has_content_but_empty` once a title exists, `x_measured_constant_never_calibrated` once a lane in the constant's scope ships, `shipping_arm_ladder_complete` and `incumbent_arm_scored` once the search names its shipping arm, `x_run_on_a_blocked_host` on active runs only; every logic CHECK carries a `CONSTRAINT` name and every `-- @check` a `-- @fix`, so a refusal's fix is authored beside its rule | the fixture is built verb by verb through the API and passes through each of those states — a precondition is not a store invariant; and the `REFUSING: <what> -- <fix>` reply is composed from the schema, never invented in the store |
| M2: `host_identity` (measurement, ROW, `@writer agent`): every identity a host's agent reported — the artifact `<flavour>:<version>` (the version carries the git sha), `harness_version`, the ffmpeg build, sha and filters, the FFVship version, free bytes; `v_host_identity_current` is the latest per host, what a plan pins and a claim is compared against; `x_run_artifact_not_reported`: a run whose `(host, artifact, harness_version)` no identity row carries | a plan is built for the code the node runs, and K1 needs the ffmpeg version string at plan time; the store keeps what was reported, the claim asks what is still reported |
| M2: `published` (measurement, ROW, `@writer exchange`): a file on the share by its share-relative path — an encode with its run and cell, or a cut, never both (`published_names_one_thing`) — with the bytes and the sha the agent computed before the copy and the hub verified after it; `x_published_without_encode`: a published cell with no encode record | the share holds products of the record, never loose files; the score planner reads it to know what a scorer on another machine can pull |
| M2: `x_score_height_not_a_served_lanes`: a score under a run with no search at a height no served lane of its class uses; ingest takes a search-less run's height from the one `lane.score_height` its class's served lanes share and refuses two | a screen or viewing run is scored without a search; I2b says every lane scores at its device's panel height, so the lane decides and two lanes need a search to choose |
| the legacy tables `import-legacy` fills are not designed here; they land at M3 as a migration | the acceptance comparison needs the archived values in the store, and none of the record's own tables may hold them |

### The verbs — `sweep <verb>`, each an endpoint

The CLI is thin: one subcommand per verb, a typed request, the reply printed. Every refusal is an
HTTP 422 whose body starts `REFUSING: <what> -- <fix>`; the CLI prints it and exits 1. Every
authoring endpoint applies its change in a transaction, runs every check and rolls back on a firing
one; every mechanical endpoint refuses while any check fires. The OpenAPI document is the API
contract, exported to `record/openapi.json`.

**Catalogue** (one endpoint per table group): `add-host` (with `machine`, `share_root`, `work_root`,
`local_view`) · `add-unit` (encoder_unit + its `host_unit`: `--host --device` by PCI path; a render
node is refused by the DDL) · `add-concept` · `add-setting` (+ enum values, roles, per-unit scope) ·
`add-lane` (+ steps) · `add-constant` (a `measured` constant takes no value; `policy` needs
`--reason`) · `scope-constant` (the lanes a constant admits — a verb of its own, because CEILING must
exist before the lanes that cite it and its scope names lanes) · `add-ladder` (+ rungs) · `author-chain`
(`--lane --host --unit --vf-template`) · `add-scorer` (host, backend, GPU, cache dir, tool paths —
intent; the build is never typed and never stored here: it is what the agent's `identify` reports
on each run) · `set-floor` · `block-host --fix` / `unblock-host`. Every write to a FILE table is a
verb's endpoint; `lane`, `host` and `search` each have an add verb and update verbs (`set-floor`,
`block-host`/`unblock-host`, `set-shipping-arm`).

**Sample and decision**: `pin-window` (a pinned window with a cut is never re-scanned) ·
`classify-cut --reason` (the `classified` `cut_check` row: the result is a row, the reason is
authored) · `define-class` (refuses a stratum with no member) · `record-viewing` (`--viewed-on` names
the device; refuses a discarded encode) · `author-search` (base arm; the coarse ladder for the locate pass; candidates
only with a HONOURED verdict on a member; the incumbent pinned at `--anchor` and naming its
acceptance viewing; targets naming theirs; the height a served lane's) · `set-shipping-arm` ·
`exclude-route` · `ship` (every step of one `(lane, host)` in one call, so routing is complete at every
moment once a lane ships; every DDL check, `measured_config_was_measured`, `content_rate_meets_floor`,
rung on the ladder, chain exists) · `calibrate --lane --title`
(HEADROOM's full-length encode) / `--from-run` (RUNG_FACTOR, BOUND, HOST_THRESHOLD; no value flag).

**Mechanical** (plan → enqueue → wait → ingest, all inside the hub; the count is `len(cells)`):
`inventory --host --library --titles` (a run on a host that mounts the library; the agent probes each
named file — `title_id=path` pairs, the id the operator's — and posts a `title` record; the library
scan that finds the files is M4's) · `propose-window --title` (recipe W1 on a node: prints the
candidate `ss` and its score; nothing is written until `pin-window`) · `materialise --adopt` (a run
on a host holding existing cuts, which the agent hashes, counts and copies into the reference set's
home under its work root; the cuts name the chain that built them, wherever that was; a second host
adopting the same set posts cut records only, and a differing content hash is refused; cutting is
M4's) · `verify` (a run per host: the content hash and the content checks) · `screen` · `locate` ·
`derive-ladders` · `encode --search [--windows --rungs --arms]` (Stage 5 from the search: the base
arm on every in-range rung on every member, each candidate on its own derived ladder, the incumbent
at its pinned anchor; the filters name subsets that must exist; a key that already exists is skipped
and reported, and nothing left is refused) / `encode --stage viewing --content-class-id --encoder-unit-id
--cells` (the operator names each cell's window and identity settings; no search) ·
`score --run [--scorer host] [--keep]` (the height is the search's, or, for a run with no search,
the one height its class's served lanes share — no height flag exists; the scorer defaults to the one
on the parent's machine, viewing its files through `local_view`; refuses a scorer whose reported
filters lack its backend's, a reference cut or an encode reachable neither on its machine nor on the
share, a second scorer on a search without an exact equivalence; `--concurrency` is M4's) ·
`publish --run | --cut-ids [--via host]` (a job on a host's queue: a run's kept encodes, or cuts,
copied to the share with a sha both ends, read through `local_view` when another runtime does the
writing) · `equivalence --search --scorers a,b [--arms …]` (the same cells on both with `--keep`;
per-frame identity for the FFVship metrics, per statistic for libvmaf; writes `scorer_equivalence`) ·
`time --run [--lane] [--repeats]` (every configuration the run encoded, on the source cut, through
the lane's chain; `--lane` when the class serves several; `--workers` and `--split` are M4's) ·
`probe --lane --unit --host --title` (its slices are the one machine-written rows of `window`,
`origin = generated`) · `watch --run-id` (server-sent events: the run's events so far, then whatever
follows to a terminal one; exits by it; never launches) · `abandon --run-id --reason` (the event the
agent reads between cells; a finished run is not abandoned).

**Read-only**: `check` · `status` (answers from the hub's state; a silent agent is reported silent,
so a downed node cannot kill it) · `rank` · `categorise` · `invert` · `render [--check]` (the build
order's generated regions, recipe E1, written into the campaign repo by the CLI; `--check` also
refuses a `(lane, host, step)` with neither a shipped row nor an exclusion — the completeness rule
`x_supported_lane_not_routed` no longer carries for unshipped lanes) · `export` · `compare-legacy`.

**Legacy**: `import-legacy` writes the archived values into the legacy tables M3 adds — a store
write, and a differing existing row is refused.

### The control plane — the hub's agent API, Redis behind it, the share beside it

Agents speak REST to the hub and nothing else; the hub is the only Redis client.

| endpoint | who | semantics |
|---|---|---|
| `GET /agents/{host}/config` | agent, at start | the host row and its scorer row: everything beyond the agent's three environment variables (`SWEEP_HUB`, `SWEEP_TOKEN`, `SWEEP_HOST`) |
| `POST /agents/{host}/claim` (long-poll, `block_s`) | agent | the hub reads the host's queue (`XREADGROUP` on `harness:queue:{host}`, consumer = host, one entry) and returns the run: the plan inline with every cell's argv, the inputs in the host's spelling, and `done`, the cells already recorded, so a resume is idempotent; a restart returns the agent's own pending entry first (`XAUTOCLAIM`) with no new event; a planned or failed run is posted `launched` here, for the artifact the host currently reports (a 409 with the fix when it differs, and the entry waits); a timing run is handed out only while its machine is quiet (204, the entry waits); a finished run's stale entry is acked away; a publish job comes back the same way with its files |
| `POST /agents/{host}/heartbeat` | agent, every 30 s | `{run_id, cells_done, cells_total, artifact, identity}` kept in Redis with a 90 s TTL; an identity that differs from the host's current row is recorded; `status` reports a silent agent as silent, never as zero |
| `POST /runs/{id}/events` | agent | `{state, detail}`, `running` or `failed`; the hub stamps `at` from its own clock, so one clock orders a run's log, and writes `by = agent`; `run.state` is the last event |
| `POST /runs/{id}/records` | agent | one record per cell as it completes, a batch in one transaction; ingested at once, so progress is the store's own count; a record posted twice is idempotent; a refused batch rolls back whole |
| `POST /runs/{id}/frames` | agent | a cell's per-frame values for one metric, kept gzipped beside the store and outside the export; the height is the run's, never a field |
| `POST /runs/{id}/ack` | agent | the hub verifies the records against the plan, stage by stage: `complete` with `verified_at`, or `failed` with what is missing (an abandoned run is released and stays abandoned); then `XACK` |
| `GET /runs/{id}/abandon` | agent | the flag checked between cells, with the reason |
| `POST /agents/{host}/ack` · `POST /exchange/published` | agent | a publish job's ack, refused until every file it named is a `published` row; the exchange's proof of one file: the sha the agent computed before the copy, which the hub checks against the file at its bind-mounted pool path before recording it |

`Queue` is an interface with `RedisQueue` and `FakeQueue` (same semantics), so every hub test runs
in-process under the FastAPI test client; the real Redis is exercised by the integration suite,
which runs in CI with a Redis service container and locally under `make integration` with Docker.
Live `watch` is server-sent events fed by Redis pub/sub.

**The share is the data plane**, under `temp/harness/` (a subdirectory of `temp/`, never the share
root): `runs/<run_id>/enc/` and `refsets/<reference_set_id>/`. Each host addresses it in its own
spelling from `host.share_root`; the hub has the pool path bind-mounted. `publish` copies to the
share and shas both ends (the agent's before the copy, the hub's after, at the pool path), and
every runtime that publishes mounts `temp/harness` read-write; `pull` copies from the share to the
work root and checks the sha the hub recorded, because a timing run must read local disk and a
transfer is proven, never assumed. Two runtimes on one machine use `host.local_view` — the owner's
work root as the viewer spells it — instead, and eta's encodes leave the machine only through its
scoring container's mount, as a publish job `--via eta-wsl`. Encodes bound for another machine and
reference sets move through the share; plans, records and events never touch it. Nothing transits
the laptop. The exchange's record is `published`: one row per file, keyed by its share path.

**The mechanical verbs on top.** `enqueue(run)`: the plan rows and the `planned` event in one
checked transaction — a blocked host, an unquiet machine and every other check refuse here — then
the `XADD`; two stores cannot share one transaction, so a queue write that fails after the commit
leaves the run `abandoned` and visible in `status`, and a claim of an entry whose run is finished is
acked away. The agent posts `running` when it starts its first cell. `wait` is the hub's own loop:
an active run whose host's heartbeat has expired is posted `failed` with the count reached, and its
entry stays for the restart; the ack is where `complete` with `verified_at` is decided, against the
plan's cells and, for a score run, its parent's kept encodes. Nothing ever reads an agent's log.

**On the node.** The agent runs `identify` at start — FFVship `--version`, the ffmpeg build's
`-version`, `-filters` and sha, the artifact from the image's stamp or the package's version, free
space under its work root — and sends it in every heartbeat; the score planner refuses a scorer
whose reported filters lack the backend's before anything is enqueued. Before any job that
publishes, the agent writes and removes a probe file under `temp/harness/` on its mount (the eta
scoring container's CIFS mount is the case this exists for) and refuses the job with the fix when
denied; the mount's credential comes from the vault through the compose role, as htpc-01's does.

**Auth and exposure.** The hub sits behind nas-01's traefik at `harness.<domain_name>` with TLS;
every client presents a bearer token from the vault — the operator's (`SWEEP_TOKEN`) and one per
agent (a JSON file `{host: token}` named by `SWEEP_AGENT_TOKENS`, never the store, which is
exported). An agent route (the `agents` tag) opens to an agent's token only, and only for the host
it names or the run's host; every other route opens to the operator's only; the wrong door is a 401
in the refusal's form.

**What ssh is still for**: `identify` on demand and diagnostics. No launch strings, no
detached-process flags, no busy probe, no per-OS counting, no file transfer by the laptop.

### The node agent — `sweep/node/`

`agent.py` (`serve`: three environment variables, the rest from `GET /agents/{host}/config`;
long-poll `claim`; per run: re-post any record on disk the hub lacks, post `running`, run every
cell not in `done` one job at a time, write each record atomically then post it, heartbeat, read
the abandon flag between cells, ack; a publish job the same way with its files; `identify`;
`hash <paths>`; `httpx` for the API, dependencies declared in the `node` extra) · `config.py` ·
`identity.py` · `ffm.py` (subprocess with argv lists, `-progress` parsing requiring `progress=end`,
framemd5 content hash and frame count, the decode probe read from stderr signatures — a probe that
did not run raises, never answers — tool versions with the ffmpeg sha computed where it runs, a bare
Windows path taken whole) · `jobs.py` (encode by frame count with bytes from the disk, or a failure
record carrying stderr; a title from ffprobe with F1's source-type heuristic; adopt: hash, count and
copy a cut into the reference set's home; recipe S1 exactly — the reference rescaled once per (cut,
geometry) and released, FFVship per-frame values pooled by nearest rank, libvmaf one pass, input 0
distorted, the CUDA or the plain graph from the scorer's `metric_backend`, the three metric steps
through `pool.run_all`, the encode discarded unless the plan keeps it; recipe T1 — repeats with the
first flagged warm-up, every leg verified by frame count, the encode discarded; pull and publish by
sha) · `pool.py` (concurrency handled once: join every future, raise the first error in submission
order) · `records.py` (atomic writes, nothing overwritten). The W1 scan, `timing --workers` and
`--split` are M4's. Records: `encode` (bytes, frames, duration, bitrate, decode_path, kept, or a
failure with stderr and rc), `title`, `reference_set`, `cut`, `score` (pooled statistics, per-step
seconds; per-frame arrays uploaded separately and kept by the hub beside the store, outside the
export), `timing` (samples with `is_warmup`, and the frame count per leg). Every argv comes from the
plan or from the one builder called with the plan's parameters.

### Data formats

- **API bodies** are typed models, one per verb, generated into the OpenAPI document; unknown
  fields are rejected (`extra = forbid`); enums and references are validated by the store's DDL and
  checks. Host tool paths are argv lists, never shell strings.
- **The plan handed to an agent** (the claim body): `run` (run_id, stage, host, node_label, unit,
  class, search, device, tools, work_root, share_root, versions, recipes, artifact) · `windows` ·
  `inputs` (per cut: the path in the host's spelling — the reference set's home under the work root,
  or a share path to `pull` with its published sha — the content sha, the frame count, and the probe
  argv for a source cut) · `cells` (cell_key, window_id, cut_kind, settings with roles, argv,
  output, keep, repeats, workers, legs) · `score` (the height, the geometry, keep, the metric
  backend, the scorer's tools; per cell where its encode and its reference are, viewed or pulled) ·
  `reference_set` and `cuts` for an adopt · `done` (the cells already recorded). A publish job is
  `{kind: publish, by_host, files: [{local, relative, run_id, cell_key, cut_id}], done}`.
- **Ingest** maps records to `encode`/`cell_failure`, `score` rows (`ssimulacra2` mean·p5·min,
  `butteraugli` max, `vmaf`/`cambi`/`psnr_y`/`float_ssim` mean, `recipe`, `scorer_build`, under the
  score run), `step_trace`, `timing`; derived tables (`setting_verdict`, `arm_ladder_rung`, `constant_value`) are
  written by the hub's own verbs.
- **The export** (the campaign repo's `record/`): `authored/<table>.json`, `sample/<table>.json` (the
  sample's ROW tables: title, reference set, cut, cut check), `agents/<table>.json` (what the agents
  reported outside any run: `host_identity`, `published`), `runs/<id>/plan.json`,
  `runs/<id>/events.jsonl`, `runs/<id>/records/*.json`, `constants.json`, `openapi.json` —
  deterministic ordering, so a re-export of unchanged state is an empty diff.

### Module map (public repo)

| file | responsibility |
|---|---|
| `sweep/hub/app.py` | the FastAPI app factory: routers, the bearer-token middleware, the two handlers that make a refusal a 422 plain-text `REFUSING` body, store and queue wired per process |
| `sweep/hub/refusals.py` | the one form, composed from the schema: the named constraints' fixes, SQLite's enum, NOT NULL, UNIQUE, foreign-key and STRICT messages read into it, a firing check with its `-- @fix` |
| `sweep/hub/api/{catalogue,sample,search,decision,runs,agents,analysis,record}.py` | one router per verb group; typed bodies; every write in a transaction that runs the checks; `runs` holds the mechanical verbs, `agents` (one router, no prefix, the `agents` tag) what an agent speaks |
| `sweep/hub/auth.py`, `sweep/hub/wait.py` | who is speaking, by token and the route's tag; the hub's loop: the expired heartbeat, the verification at ack |
| `sweep/hub/store.py` | SQLite from `sweep/schema.sql` (WAL, one writer, the schema pinned by `user_version`); every write one transaction that runs `model_check.run_checks`; `require`, `insert`, `RETURNING` ids, `plan_run`, `post_event`, `calibrate` |
| `sweep/hub/ingest.py` | a posted record → rows |
| `sweep/recipes.py` | pure functions named by recipe: `G1 panel`, `K1 cell_key`, `V1 verdict`, `T1 spread/nstar`, `R1 beats`, `nearest_rank`, `bd_rate` (ported from `analyze.py:140`); shared by hub and node |
| `sweep/hub/build.py` | the one command builder: encode argv per frontend (from `sweep.py:690 build_encode_args`), production argv from `chain.vf_template`, legs as prefix truncation, cut/probe/rescale/FFVship/libvmaf argv |
| `sweep/hub/planner.py` | stage planners → plan rows and the claim body; run ids `<stage>-<subject>-<UTC stamp to the microsecond>`; `enqueue`, `done_cells`, `claim_body` |
| `sweep/hub/queue.py` | `Queue`, `RedisQueue` (streams, consumer groups, heartbeat TTLs, pub/sub), `FakeQueue` |
| `sweep/hub/exchange.py` | the share in each host's spelling; the hub's bind-mounted view; `publish`/`pull` with sha both ends |
| `sweep/hub/artifact.py` | the identity each agent reports, recorded when it changes (`host_identity`), and what a plan pins from the current one |
| `sweep/hub/analysis.py` | rank (per window `bd_rate` over the shared range → median, k of n, per stratum), categorise (per decode path, UNMEASURED), invert on `lane.decision_rule` (tightest straddling pair, from `analyze.py:324`), content rate, screen and admissibility verdicts, `derive_ladders` (from `settings_search.py:299 locate_ladders`), calibrate (BOUND from `m4_routing.py:57-102`), equivalence, the comparison primitive |
| `sweep/hub/render.py`, `sweep/hub/legacy.py`, `sweep/hub/export.py` | E1; `import-legacy` and `compare-legacy` (the campaign repo's committed CSVs, uploaded by the CLI); the deterministic export |
| `sweep/cli/__init__.py` | `sweep`: one subparser per verb (the verb table proven equal to the OpenAPI document, field by field), `httpx` to the hub, prints replies, exits 1 on `REFUSING` and 2 when the hub is unreachable; `export`, `render` write into the campaign repo; run as `uvx --from git+…@<tag> sweep` or `uv tool install` |
| `sweep/node/{agent,config,identity,ffm,jobs,pool,records}.py` | above; the same package in all three node images and, via `uvx`, natively on eta |
| `sweep/schema.sql`, `sweep/model_check.py` | unchanged in role: the schema is the source, the proof and the doc rendering stay |
| `docker/Dockerfile.hub`, `docker/Dockerfile.node-encode`, `docker/Dockerfile.node-encode-mesarc`, `docker/Dockerfile.node-score` | the images above; `uv.lock` is the only place third-party packages are pinned, and every image installs from it |
| `.github/workflows/ci.yaml`, `images.yaml` | tests + `model_check --mutate` + docs `--check` + integration against a Redis service; build and push the four images to GHCR on main and tags (the FFVship CUDA build stage cached), each stamped with the checkout's version and sha; `docker/README.md` says what a role gives a container |

Lifted verbatim from the archived harness, cited at the call site: `parse_progress` (`sweep.py:1013`), `nearest_rank`
(`:1065`), `parse_ffvship_json` (`:1083`), `rescale_lossless` (`:1879`), `libvmaf_graph`/`score_libvmaf`
(`:2266`, `:2286`), `can_hw_decode` (`:2377`), `content_hash` (`:3838`), `measure_noise_floor`
(`:3409`), N\* (`encode_run.py:1421`), the sha-on-both-ends rule (`push_node.py:105`), the FFVship
build (`ffvship/Dockerfile.cuda`). The launch strings, the busy probe and the `scp -3` relay are
not lifted: the queue makes them unnecessary.

### Scoring on more than one host

The campaign harness centralised scoring on one host. That rested on two facts: FFVship existed nowhere else, and the nodes carried different libvmaf builds. The rewrite
closes the second by construction — every score row carries `scorer_build`, and the store refuses
to mix scorers on one search without a measured equivalence — and the node-score image closes the
first: the same FFVship and the same `libvmaf_cuda` build on media-01-score and eta-wsl.

**The acceptance bar is bit-identical, and the record says it is fair:** FFVship is bit-deterministic
on one GPU, every frame, raw JSON sha equal across runs (measured on the archived harness). With one image on both scorers the only variable is the GPU, Ampere against Blackwell, for
FFVship and `libvmaf_cuda` alike, unknown until measured. **The rule:** a search is scored by one
scorer unless `scorer_equivalence` is `exact` for `ssimulacra2` and `butteraugli` between the two;
then its cells may be split. Different searches may always go to different scorers — eta-wsl
scores what eta encodes through `/mnt/d` with no copy; media-01-score scores the B580 and the
A4000. Scoring B580 encodes on eta costs one publish and one pull, ~25 GB per 360-cell search over
the LAN. Encodes made on eta leave the machine only through the scoring container's CIFS mount and
only when a policy asks; by default they never do. **Cross-cell concurrency on eta**
(`score --concurrency N`) is a measured knob with default 1.

### How the `by_construction` class stays closed

No mechanical or record endpoint takes a count, a device, a directory, a height or a card name as
free input: the count is `len(cells)`; the device comes from `host_unit`; the height from
`search.score_height`; the unit from the run row; nothing but the API can write the store; an agent
runs only what its claim hands it, for the artifact the plan names. The catalogue verbs are where
those are authored once — `add-unit --device`, `add-host`'s roots, `add-scorer`'s cache dir. The
table below carries one row per refusal id; `test_refusals.py` asserts every `by_construction` id
appears in that table and that no request body outside the catalogue router carries a field named
`count`, `device`, `height`, `directory` or `root` in the OpenAPI document. Honest labelling: the
name test proves those words are absent from the mechanical bodies, not that no free input of those
kinds exists; the by-construction claim rests on the verbs' shapes above. The agent router is the
second exemption: its bodies are observations — a title's height, a frame count — posted by the
node about what it saw, not inputs the operator chooses.

The other twenty-five refusals are not closed here: `check` ones are `x_*` views or script checks the
store runs on every write, and `process` ones are rules a stage applies. This table is only the class the shape
itself makes impossible.

**`identity_and_provenance`**

| refusal | what it protects | why it cannot happen |
|---|---|---|
| `id-no-cross-card-fallback` | identity never falls back to another card's tool | the unit is `run.encoder_unit_id`, taken from the catalogue at plan time; an agent reports its identity but never names a unit, and `x_run_unit_not_on_host` keeps it in its box |
| `id-device-by-slot` | the card is addressed by PCI slot | `host_unit.device` is written once by `add-unit` — the one body that carries a device — and read by the builder; no mechanical body and no plan carries one, and the DDL refuses a render-node number |
| `id-record-matches-profile` | a record written for another node is refused | a record is posted against the run that was claimed and ingested under its unit by foreign key; there is no file to mis-attribute |
| `id-scorer-in-the-key` | the metric backend is part of the scorer's identity | the scorer row is intent; the build is observed — `run.scorer_build` from the agent's `identify`, never typed — and `score`'s primary key carries `scorer_build` and the run |
| `id-scoping-reaches-every-builder` | scoping reaches every command builder | `sweep/hub/build.py` is the one builder and it reads `host` and `host_unit`; an agent composes no flags, so there is no second builder to miss |
| `id-a-constant-says-how-its-value-was-decided` | a constant carries the provenance of its value | `add-constant` refuses a value on a `measured` constant and a `policy` one without `--reason`; `calibrate` is the only writer of `constant_value`; `x_measured_constant_never_calibrated` |

**`admissibility`**

| refusal | what it protects | why it cannot happen |
|---|---|---|
| `adm-read-stderr` | read stderr, never the exit status | a failed cell is posted as a failure record carrying stderr; `cell_failure.stderr` is NOT NULL and `rc` gates nothing |
| `adm-guards-are-per-vendor-and-codec` | a capability verdict is scoped to (vendor, codec) | `setting_verdict` and `arm_setting` are keyed by `encoder_unit`; no verb takes a card name or a vendor string |
| `adm-no-silent-fallback` | an unrecognised backend, codec or encoder refuses | enum CHECKs and foreign keys run inside the write transaction; the reply is a 422 `REFUSING`, and there is no default to fall back to |
| `adm-exclude-with-a-reason` | a wholly inadmissible block is excluded with a written reason | `setting_verdict.reason` is required when the verdict is EXCLUDED, and `exclude-route` requires one too |

**`measurement_validity`**

| refusal | what it protects | why it cannot happen |
|---|---|---|
| `val-software-decode-is-not-hardware` | a software-decoded number is never averaged into a hardware one | the agent measures the decode path per window from stderr signatures; `timing.decode_path` is NOT NULL and the analysis partitions on it |
| `val-none-is-not-zero` | "not given" and "explicitly serial" stay distinct | typed request bodies over STRICT columns; nothing between agent and store is a blank field in a text file |
| `val-discard-the-warmup-sample` | the warm-up sample is flagged, not silently dropped | the agent posts every sample with `is_warmup`; the content-rate read filters it |
| `val-never-compare-encodes-by-hash` | framemd5 for pixels, bytes for size | `cut.content_sha` is a frame hash by construction; the exchange shas files to prove a transfer and nothing compares encodes |
| `val-real-content-not-synthetic` | screens run on real content of the class | every cell names a cut, and every cut names a title; `v_setting_unit_reading` needs the whole class before it will say INERT |

**`the_artifact_must_come_home`**

| refusal | what it protects | why it cannot happen |
|---|---|---|
| `art-extra-distinguishes-the-arms` | every field in the cell key survives into every reading | settings are `cell_setting` rows read through the comparison primitive; there is no CSV column to lose |
| `art-move-stale-output-aside` | a previous output is never mistaken for this run's | products are rows under a new `run_id`; there is no whole-file producer, and a posted record is idempotent rather than overwriting |
| `art-wait-on-the-artifact-not-the-log` | wait on the artifact, not a log, and detect a dead job | `wait.py` is the hub's own loop over the heartbeat TTL, and the ack verifies the records against the plan; nothing reads an agent's log |
| `art-check-staging-before-copying-into-it` | a leftover staging directory is checked before use | the share is addressed as `runs/<run_id>/`, allocated per run, and a cell is addressed by its key |
| `art-never-glob-a-shared-directory` | drive from recorded paths, never a glob | the plan names every input path in the host's spelling, and the exchange copies by name |
| `art-file-by-header-fingerprint` | an arriving table is filed by its content, not by the operator | nothing arrives as a table: an agent posts records to `/runs/{id}/records` and ingest maps them to rows |

**`orchestration`**

| refusal | what it protects | why it cannot happen |
|---|---|---|
| `orc-the-orchestrator-is-make` | node-touching work has one entry | the only path to a node is enqueue → claim; an agent runs what its claim hands it and has no other input |
| `orc-host-wiring-is-not-the-descriptors` | binaries and devices come from the host profile | they are `host` and `host_unit` columns; the plan carries argv the builder already resolved, and no request body takes a binary or a device |
| `orc-the-invocation-is-committed` | the invocation exists afterwards; the count is derived | the run and its planned cells are the invocation, exported to `record/`; the count is `len(cells)` and no endpoint accepts one |
| `orc-status-must-not-die-on-a-downed-node` | status is read-only and survives an unreachable node | `status` answers from the store and the heartbeat TTL; a silent agent is reported silent, which is a different answer from zero |

**`the_search`**

| refusal | what it protects | why it cannot happen |
|---|---|---|
| `search-one-ladder-per-candidate-and-window` | a ladder is per (arm, window) | `arm_ladder_rung` is keyed that way, written only by `derive-ladders` from the search's own locate run |
| `search-range-from-admissible-arms-only` | the shared range is set by admissible arms only | `x_arm_setting_not_honoured` — an arm carrying an unhonoured setting cannot exist to widen it |
| `search-speed-partitions-by-measured-decode-path` | speed aggregates within a measured decode path | the path is measured per window and stored on the timing row; no endpoint accepts a window-exclusion list |
| `search-admissibility-excludes-never-ranks` | an inadmissible arm is excluded, never ranked last | it cannot enter the search, and `rank` reads its arms from the store |
| `search-separate-ledger-for-unranked-rungs` | rungs a ranking never saw stay separate | extension rungs are `ladder_rung` rows of the codec ladder, a different table from `arm_ladder_rung` |

### Testing

`uv run make check` is the gate, after `uv sync --locked --extra hub --extra cli`: the named unittest
modules, `sweep/model_check.py --mutate` (every check fires on its negative case) and `--check` (the
rendered regions of `DATA-MODEL.md` are current). Modules are named, never discovered. Above that:

- **Hub tests run in-process** under the FastAPI test client with `FakeQueue` and a temporary store,
  so every verb, every refusal and every ingest path is covered without a container.
- **Every refusal has a test that names it**, and `test_refusals.py` asserts coverage against
  `refusals.json`: every `by_construction` id appears in the table above, every `check` id maps to an
  `x_*` view or a script check the model proves, no request body outside the catalogue router
  carries a `count`, `device`, `height`, `directory` or `root` field in the OpenAPI document, and
  `REFUSING:` is spelled in one place in the code — everything else composes through it.
- **The replay is the acceptance of the hub itself**: `test_hub_replay.py` builds the proof's fixture
  through the API and the ingest path in the campaign's order (86 steps), with no check firing at any
  step and the authored tables equal to the fixture's row for row; a re-export is an empty diff.
- **The agent runs end to end in-process**: `tests/node_helpers.py` authors the smallest catalogue
  through the API with the fake `ffmpeg`, `ffprobe` and `FFVship` under `tests/fake_tools/` as the
  host's binaries, makes the sample through the agent's own inventory and adopt jobs, and
  `test_node_agent.py` drives claim → records → ack for every stage, the killed agent's resume, the
  refused artifact, abandon and a publish.
- **The integration suite** exercises `RedisQueue` (the same contract mixin `FakeQueue` passes) and
  the agent protocol on it against a Redis service container — in CI on every push, locally under
  `make integration` with `SWEEP_TEST_REDIS` set and a Redis in Docker.
- **Mutation checking**: a suite that cannot fail on a deliberate break is not evidence.

### The acceptance procedure

The harness is accepted when it reproduces the campaign's committed values, not when its tests pass.

1. `import-legacy` loads the archived harness's committed CSVs for the B580 `av1_qsv` column into
   the legacy tables (M3); the row set compared is fixed before the run, and the rows of an arm the
   search excluded are compared or refused by name, never dropped. `materialise --adopt` registers
   the existing cuts by content hash and refuses a differing one.
2. The same cells are re-encoded and re-scored under recipes S1 and K1 on the same reference pixels.
3. `compare-legacy` requires **exact** equality for every stored statistic — the scorers are
   bit-deterministic on one GPU, so "the same" means the same number, not a tolerance.
4. **Timing is compared as ratios**, never as absolute seconds: the box, the driver and the
   contention are not the archived run's.
5. A search may be scored by two scorers only once `equivalence` has measured them `exact` for
   `ssimulacra2` and `butteraugli`; until then one scorer per search.

---

