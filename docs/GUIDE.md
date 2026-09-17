# Using the harness

How to drive `sweep` from an empty hub to measurements you can read. The other four documents say
what the harness measures (`SPEC.md`), how the record is shaped (`DATA-MODEL.md`), how the pieces fit
together (`ARCHITECTURE.md`) and which invariants exist and why (`refusals.json`). This one says
which commands to type, in what order, and what comes back.

**What is checked and what is not.** A test holds this document's *verb list* against the CLI, in
both directions: a new verb cannot land without a line here, and a command here cannot name a verb
the CLI has dropped. Nothing checks the prose. Every reply printed below was captured by running the
command against a real store, but the surrounding explanation can go stale like any other text — if
it disagrees with `sweep <verb> --help` or with `ARCHITECTURE.md`, they are right and this is wrong.

Hosts here are `box-a`, `box-a-score` and `nas`; the paths are illustrative. Substitute your own.

---

## 1. What you need

A running hub, its URL, and a token. Deployment is not this document's subject: `docker/README.md`
says what a role must give each container, and `ARCHITECTURE.md` says how the hub, the queue, the
share and the agents fit together.

```sh
uv tool install "gpu-encoder-sweep[cli] @ git+https://github.com/andyattebery/gpu-encoder-sweep@<tag>"

sweep --hub https://sweep.example --token "$TOKEN" config --save
sweep status
```

`config --save` writes the two values to `~/.config/sweep/env` at mode `0600`, and every later command
reads them from there — so a fresh terminal needs no exports and the token does not go in a shell rc
file. `XDG_CONFIG_HOME` is honoured, and `$SWEEP_CONFIG` points somewhere else entirely if you keep
more than one hub. The file is plain `KEY=value` and hand-editable; it can even be `source`d, since a
leading `export ` is accepted:

```
# ~/.config/sweep/env, mode 0600
SWEEP_HUB=https://sweep.example
SWEEP_TOKEN=…
```

**Each value resolves in this order: the flag, then the environment, then the file**, with the hub
falling back to `http://127.0.0.1:8000`. The environment beating the file is what keeps a one-off
`SWEEP_HUB=… sweep status` working — and the reason `sweep config` reports where each value came
from rather than only what it is:

```
$ sweep config
hub    https://sweep.example      ~/.config/sweep/env
token  set                        ~/.config/sweep/env

$ SWEEP_HUB=https://other.example sweep config
hub    https://other.example      environment
token  set                        ~/.config/sweep/env
```

If a command is reaching the wrong hub, that second form is how you find the stale export doing it.
The token is reported as `set` or `unset` and never printed.

**A config file anyone but you can read is refused, not warned about** — it holds a bearer token:

```
$ sweep config
REFUSING: ~/.config/sweep/env holds a token and is readable by more than its owner -- chmod 600 ~/.config/sweep/env
```

`--save` will not quietly fix that, because tightening the file would mean reading it first and a
file others can read is exactly what must not be read. Run the `chmod`, then save.

**Exit codes.** `0` the hub accepted it · `1` the hub refused it · `2` the hub was unreachable. So
`sweep … && next-thing` does what you would expect in a script. `sweep watch` is the exception: it
exits by the run's final state, `0` complete and `1` failed or abandoned.

**Where the examples come from.** Every reply shown was captured by running the command against a
real store — the paths and host names are this document's, the shapes and the refusal texts are not
invented. The test suite drives the same paths and is the executable reference when a sequence here
is unclear: `tests/replay_data.py` is the whole authoring sequence as verb bodies, and
`tests/node_helpers.py` builds a two-runtime fleet against fake `ffmpeg`, `ffprobe` and `FFVship`, so
a campaign runs end to end in about a second.

---

## 2. Every verb works the same way

One subcommand per endpoint; every flag is one field of the request body. Nothing is positional.

```sh
sweep add-host --host box-a --ssh-host box-a --os linux --machine box-a \
               --work-root /srv/sweep --share-root /share
```

**Types are checked at the CLI before the request is sent.** A list flag takes a comma-separated
value (`--lanes a,b`), a nullable number takes `null` to mean NULL (`--min-content-rate null`), a
boolean takes `true` or `false`, and a nested body takes raw JSON (`--scope '[{"…": …}]'`).

**`--from FILE` supplies the whole body as JSON, and flags override it.** This is how anything with
more than a few fields is written, and how the fleet is kept under version control:

```sh
sweep add-setting --from settings/qsv.q.json --kind ordinal   # the file, with kind replaced
```

**A refusal is one line and it tells you the fix.** Every refusal is an HTTP 422 whose body is
`REFUSING: <what> -- <fix>`, printed as is:

```
$ sweep add-host --host box-b
REFUSING: add-host needs 'ssh_host' -- give --ssh-host

$ sweep add-scorer --host ghost --ffvship '["/usr/local/bin/FFVship"]' …
REFUSING: host host='ghost' does not exist -- add it first with add-host
```

The half after `--` is not advice, it is the action that resolves it. Refusals come from three
places: the request body (a missing or mistyped field), the store's own constraints, and the model's
checks — and a check refusal names the check, an example row, and any other checks firing at the
same time. Nothing is half-applied: every verb is one transaction that runs every check and rolls
back if any fires.

**`sweep check` asks the store whether anything is wrong right now**, independently of what you are
doing:

```
$ sweep check
{"ok":true}
```

Not `{"ok":true}` means one or more checks are firing, each with its rows and its fix. This is the
first thing to run when something downstream refuses for a reason that makes no sense.

---

## 3. The fleet

A runtime is a `host`: an encode container, a score container, a native install. Several can share
one machine, and `machine` is what groups them — the rule that a timing run gets a quiet box is per
machine, not per host.

**Write the fleet as one document and apply it.** This is the path to prefer; the single-row verbs
below are what it is made of.

```json
{
  "hosts": [
    {"host": "nas", "ssh_host": "nas", "os": "linux", "machine": "nas",
     "work_root": "/data", "share_root": "/share",
     "notes": "the hub's own runtime: no encoder, no agent"},
    {"host": "box-a", "ssh_host": "box-a", "os": "linux", "machine": "box-a",
     "work_root": "/srv/sweep", "share_root": "/share",
     "ffmpeg": "/opt/jellyfin-ffmpeg/bin/ffmpeg"},
    {"host": "box-a-score", "ssh_host": "box-a", "os": "linux", "machine": "box-a",
     "work_root": "/srv/sweep-score", "share_root": "/share", "local_view": "/srv/sweep"}
  ],
  "units": [
    {"encoder_unit_id": "intel-arc-ihd26-qsv-av1", "vendor": "intel", "card": "Arc B580",
     "driver": "iHD 26.2.2", "frontend": "qsv", "codec": "av1",
     "host": "box-a", "device": "/dev/dri/by-path/pci-0000:03:00.0-render"}
  ],
  "scorers": [
    {"host": "box-a-score", "ffvship": ["/usr/local/bin/FFVship"],
     "score_ffmpeg": ["/opt/jellyfin-ffmpeg/bin/ffmpeg"],
     "metric_backend": "libvmaf_cuda", "gpu_id": 0, "cache_dir": "/srv/sweep-score/cache"}
  ]
}
```

```
$ sweep apply --from fleet.json --dry-run true
{"created":{"hosts":["box-a","box-a-score","nas"],"units":["intel-arc-ihd26-qsv-av1@box-a"],
 "scorers":["box-a-score"]},"updated":{…},"removed":{…},"unchanged":0,"applied":false}

$ sweep apply --from fleet.json
… "applied":true

$ sweep apply --from fleet.json
… "unchanged":5,"applied":true
```

`--dry-run true` does the same writes and runs the same checks, then rolls back — so it answers
"would this be valid", not only "what would change". Applying an unchanged document is a no-op, which
is what makes the file the source of truth rather than the database.

**A group left out is not managed; an explicit `[]` means there are none.** A document with no
`scorers` key removes no scorer. `"scorers": []` removes them all.

**What can still change.** A row is editable while nothing measured points at it. Once a run, a
publish or a reference set names it, every column freezes except `notes` and `ssh_host`:

```
$ sweep apply --from fleet.json           # after moving work_root
REFUSING: host 'box-a' work_root differs ('/srv/sweep' -> '/elsewhere'), and it is named by
4 published rows, 1 reference_set row, 4 run rows -- a measurement keeps the paths it ran on:
add a host row carrying the new value and plan against that, or abandon what names this one;
notes and ssh_host stay free
```

That is deliberate. A measurement keeps the paths it ran on and the device it addressed; changing
them under it would silently reattribute finished work. A driver upgrade is likewise a new
`encoder_unit_id`, never an edit — the driver is part of what a unit *is*.

**Removing is stricter than editing.** A row may not go while anything at all points at it, authored
or measured, because that would orphan the row that points back:

```
REFUSING: the document does not carry the placement of encoder_unit 'intel-arc-ihd26-qsv-av1@box-a',
which is named by 1 chain row -- put it back in the document, or remove what names it first;
nothing is orphaned to make a document true
```

**The single-row verbs.** `apply` is built from these, and they remain useful for a one-off:
`add-host`, `add-unit` (the unit's identity and the host it sits in — one unit in two boxes is two
calls with the same id), and `add-scorer` (the *intent* of scoring on a host: which binaries, which
backend, which GPU, where the cache goes — the build that actually ran is recorded per run from what
the agent reports, never typed here).

**Taking a host out of service** is not a document edit, because it is operational rather than
authored — it has its own pair of verbs, and the fix you give is quoted back at whoever tries to use
the host:

```sh
sweep block-host --host box-a --fix "the card fell off the bus; reseat it and reboot, then clear this"
sweep unblock-host --host box-a
```

---

## 4. What the encoder can do

Before a measurement there has to be a vocabulary: what the knobs are, what they mean across
vendors, and what the lane you are shipping into requires.

**Concepts** name a thing that several vendors spell differently, so a verdict about one can be read
against another. **Settings** are the flags themselves, each scoped to the units it applies to, with
the concepts it carries:

```sh
sweep add-concept --canonical-id quality_anchor --description "the knob a ladder sweeps"

sweep add-setting --setting-id qsv.q --flag -q:v --frontend qsv --kind quality_anchor \
  --subsystem rate_control --value-type int --range-lo 1 --range-hi 51 --is-generic false \
  --roles quality_anchor,rate_control_mode \
  --scope '[{"encoder_unit_id": "intel-arc-ihd26-qsv-av1", "applies": true, "default_is_measured": false}]'
```

One flag can carry two concepts — `-q:v` on qsv both selects the rate control mode and *is* the
quality anchor — which is why `--roles` is a list. A setting's `--scope` says which units it applies
to and what each one's default is; a default marked `default_is_measured` was observed rather than
read from documentation.

**A ladder** is the set of anchor values a shipped setting may take, one per codec and never per
host. **Constants** are the numbers a transcode flow's own logic consumes; each carries where its
value came from, and a `measured` one has no typed value at all because it comes from a calibrate
run:

```sh
sweep add-ladder --ladder-id av1 --codec av1 --rungs 20,24,28,32,36,40

sweep add-constant --name CEILING --unit Mbps --provenance derived --value 22.6 \
  --inputs-json '{"bytes": 512e9, "hours": 50}' --precision "+-~5%"
sweep scope-constant --name CEILING --lanes tablet-1080p-sdr
```

`scope-constant` is a separate verb because a constant has to exist before the lanes that cite it,
and its scope names those lanes.

**A lane** is one row of what you ship: the content it matches, the output it produces, how its value
is decided, and the height it is scored at.

```sh
sweep add-lane --lane tablet-1080p-sdr --codec av1 --decision-rule cap --input-width-max 1920 \
  --input-dynamic-range sdr --output-resolution 1080p --output-dynamic-range sdr --hdr-handling n/a \
  --audio copy --subtitles copy --score-height 1548 --bitrate-cap-binds always \
  --bitrate-cap-constant CEILING --has-content true --steps quality-target-encode

sweep set-floor --lane tablet-1080p-sdr --min-content-rate 2.0
```

`--decision-rule` is how the shipped value gets chosen — `cap` against a ceiling, `target` at a
score, or `incumbent`. `set-floor` is the throughput a host must reach to be worth using for the
lane; `null` means report it but do not enforce it.

**A chain** is the production filter graph for one (lane, host, unit). It is authored *before* the
sample, because the reference cut is built through it and the timing stage times it:

```sh
sweep author-chain --lane tablet-1080p-sdr --host box-a \
  --encoder-unit-id intel-arc-ihd26-qsv-av1 --vf-template "scale=1920:-2"
```

---

## 5. The sample

The sample is the content a measurement is *about*. Getting it wrong is the failure this harness was
built after, so the path has more steps than it first looks like it needs.

**Scan the library.** `inventory` is a run: the hub plans it, an agent probes each file and posts a
record per title.

```
$ sweep inventory --host box-a --library tv \
    --titles '[{"title_id":"tng","path":"/library/tng.S01E01.mkv"},
               {"title_id":"parks","path":"/library/parks.S01E01.mkv"}]'
{"run_id":"inventory-box-a-20260917T051935186183Z","entry_id":"1"}
```

**Pin the windows.** A window is a cut of a title — `(title, ss, t)` and nothing else. Choosing one
is machine-proposed and then hand-pinned, and pinning is authorship: a pinned window that already has
a cut is never silently re-scanned.

```sh
sweep pin-window --window-id tng --title-id tng --ss 120 --t 60 \
  --character dialogue --selected-by "satavg+cuts v1"
```

**Materialise the cuts.** This stages the reference and source cuts on a host and hashes them, so
every later encode is provably about the same pixels. `--chain-*` names the chain the cuts were built
through, which may belong to a different host than the one staging them.

```
$ sweep materialise --host box-a --encoder-unit-id intel-arc-ihd26-qsv-av1 \
    --reference-set-id stage-1080p --geometry 1920x1080 --pix-fmt p010le \
    --chain-lane tablet-1080p-sdr --chain-host box-a --chain-unit intel-arc-ihd26-qsv-av1 \
    --cuts '[{"window_id":"tng","kind":"reference","path":"/stage/tng.ref.mkv"}, …]'
{"run_id":"materialise-stage-1080p-20260917T051935249374Z","entry_id":"2"}
```

**Record what was checked about a cut.** A content check's result is a row; the reason it is kept
despite a finding is authorship, and a classified result without one is refused:

```sh
sweep classify-cut --cut-id tng.ref --check-name content \
  --reason "adopted from an earlier staging; content-checked then" --checked-at 2026-09-16
```

**Define the class.** A content class is the named set of windows a verdict is *keyed to*. It names
the lanes it serves and the strata its coverage is checked against. The window set is not a sample to
economise on — it is the content class.

```sh
sweep define-class --content-class-id native-1080p-sdr --name "native 1080p SDR" \
  --reference-set-id stage-1080p --lanes tablet-1080p-sdr --members tng,parks \
  --strata '[{"stratum":"hd","kind":"inventory","definition":"t.width >= 1920","min_windows":1}]'
```

---

## 6. Running a measurement

Encoding happens on a node, so every encoding verb plans a run, puts it on that host's queue and
returns immediately. The agent claims it, runs each cell, posts a record per cell as it finishes, and
acks; the hub verifies the records against the plan before it calls the run complete.

**A viewing run** encodes exactly the cells you name — the shape to use when you know the settings
you want and are not sweeping a ladder:

```
$ sweep encode --content-class-id native-1080p-sdr --encoder-unit-id intel-arc-ihd26-qsv-av1 \
    --host box-a --stage viewing \
    --cells '[{"window_id":"tng","settings":{"qsv.q":"24","qsv.preset":"4","qsv.b_strategy":"-1"}}, …]'
{"run_id":"viewing-native-1080p-sdr-20260917T051935380089Z","entry_id":"3"}
```

A run id carries its stage, its subject and the microsecond it was planned, so it sorts and never
collides. Keep it — every later verb takes it.

**Watch it, or poll it.** `sweep watch --run-id <id>` streams the run's events as they happen and
exits by the final state. `sweep status` is the whole fleet at a glance:

```
$ sweep status
{"runs": [{"run_id": "viewing-native-1080p-sdr-20260917T051935380089Z", "host": "box-a",
           "stage": "viewing", "state": "complete", "planned_total": 4, "still_planned": 0,
           "encoded": 4, "scored": 0, "timed": 0, "failed": 0}, …],
 "hosts": [{"host": "box-a", "blocked": null}, …],
 "queue": {"box-a": 0, "box-a-score": 0, "nas": 0},
 "heartbeats": {"box-a": {"run_id": null, "cells_done": 0, "cells_total": 0,
                          "artifact": "uvx:0.1.dev53+g309015ea0.d20260908",
                          "at": "2026-09-17T05:19:35.495357+00:00"}, …},
 "identities": {"box-a": "uvx:0.1.dev53+g309015ea0.d20260908", "nas": null}}
```

The counts are the store's own, not the agent's word for it. A silent agent reads as silent — its
heartbeat goes `null` rather than reporting zero progress.

**Every event says who wrote it:**

```json
{"state": "planned",   "by": "hub",   "detail": "planned"}
{"state": "launched",  "by": "hub",   "detail": "claimed by box-a; artifact uvx:0.1.dev53+g309015ea0.d20260908"}
{"state": "running",   "by": "agent", "detail": "first cell started"}
{"state": "complete",  "by": "hub",   "detail": "verified against the plan: every cell recorded"}
```

**A run you no longer want:**

```sh
sweep abandon --run-id viewing-native-1080p-sdr-20260917T051935380089Z --reason "wrong settings"
```

The agent reads the flag between cells and stops. A run that already finished is not abandoned:

```
REFUSING: run viewing-native-1080p-sdr-20260917T051935380089Z is complete -- a finished run is not abandoned
```

**When an agent will not take work**, ask it what it thinks it is. `sweep-node` is the agent's own
CLI, run on the node with the same three environment variables the role gives it
(`SWEEP_HUB`, `SWEEP_TOKEN`, `SWEEP_HOST`):

```
$ sweep-node identify
{"artifact": "uvx:0.1.dev53+g309015ea0.d20260908",
 "harness_version": "g309015ea0",
 "ffmpeg_build": "8.1.2-Jellyfin",
 "ffmpeg_sha": "34161af87f7a3816e966a53883543945fc4f8b390be6354baeb98ba60281aade",
 "ffmpeg_filters": ["format", "hwupload", "hwupload_cuda", "libvmaf", "libvmaf_cuda", "scale"],
 "ffvship_version": null,
 "free_bytes": 271174815744}
```

The hub hands a run only to an agent reporting the artifact the run was planned for, so a 409 at
claim time and a run stuck at `planned` usually means this output disagrees with the plan. Its other
two subcommands are `sweep-node serve`, the loop a role runs at boot, and `sweep-node hash PATHS`,
which prints the content hash and frame count of a file — the same hash the harness keys cuts by.

**A killed agent resumes.** Nothing needs doing: its heartbeat expires, the hub posts the run
`failed` with the count it reached and keeps the queue entry; when the agent comes back it claims the
same entry, is told which cells already have records, and encodes only the rest.

---

## 7. Scoring and timing

Scoring is its own run against a parent, on a host with a scorer row — usually a different runtime on
the same machine, which reads the encoder's work root through `local_view` and copies nothing.

```
$ sweep score --run-id viewing-native-1080p-sdr-20260917T051935380089Z --keep true
{"run_id":"score-viewing-native-1080p-sdr-20260917T051935380089Z-20260917T051935497451Z","entry_id":"4"}
```

`--keep true` keeps the encodes after scoring; without it each is deleted once its score is recorded,
which is what you want for a ladder of hundreds. `--scorer` picks the runtime when the machine has
more than one. The height is not a flag: it comes from the search, or — for a run with no search —
from the single height the class's served lanes share. Two served heights and no search is a refusal,
because the height is a decision and nobody should be guessing it.

**Timing is a separate run** because it needs a quiet machine. It re-encodes through the lane's
chain, repeatedly, and the first sample is flagged as warm-up rather than averaged in:

```
$ sweep time --run-id viewing-native-1080p-sdr-20260917T051935380089Z \
    --lane tablet-1080p-sdr --repeats 3
{"run_id":"time-viewing-…-20260917T051935828135Z","entry_id":"5"}
```

`--lane` names which chain to time when the class serves more than one. The quiet machine is enforced
twice, in two different ways: planning a timing run onto a machine that is busy right now is refused
outright, and if the machine becomes busy afterwards the agent is simply not handed the run until it
is quiet again — the entry waits in the queue rather than being lost.

**Moving bytes between machines** is a publish job, not a run. The agent copies each file to the
share and hashes it before the copy; the hub hashes it again at its own view of the share before
recording it, so a transfer is proven rather than assumed:

```
$ sweep publish --run-id viewing-native-1080p-sdr-20260917T051935380089Z --via box-a
{"entry_id":"6"}
```

`--via` is the runtime that does the writing, which matters when the owner of the files cannot write
to the share itself. `--cut-ids` publishes reference cuts instead of a run's encodes.

---

## 8. Deciding what ships

**A viewing verdict is a person's judgement, recorded**: which two cells, on what device, by whom,
and what they concluded.

```
$ sweep record-viewing --kind pair --window-id tng --cell-a <cell> --cell-b <cell> \
    --viewed-on tablet --viewer andy --verdict same --viewed-at 2026-09-16
{"viewing_id":1}
```

**Shipping** writes the value a transcode flow will run, for one (lane, host), every step in one
call. A `measured` row has to be a configuration that was actually measured — this is the check that
exists because values have been shipped that nothing ever encoded:

```
$ sweep ship --lane tablet-1080p-sdr --host box-a --rows '[…qsv.q 28…]'
REFUSING: measured_config_was_measured: a `measured` shipped row's identity settings equal some
cell's identity settings, on the shipped unit, in the evidence class, e.g. (1, 'tablet-1080p-sdr',
[('qsv.b_strategy', '-1'), ('qsv.preset', '4'), ('qsv.q', '28')]) (content_rate_meets_floor also
fires) -- ship identity settings a cell in the evidence class was encoded with on that unit, or
ship them as policy with the reason
```

Ship a configuration that was measured, and it lands. `--rows` carries one object per step of the
lane, each naming its provenance, who decided it, the class the evidence came from, and the settings
themselves:

```
$ sweep ship --lane tablet-1080p-sdr --host box-a --rows '[
    {"step": "quality-target-encode", "encoder_unit_id": "intel-arc-ihd26-qsv-av1",
     "provenance": "measured", "decided_by": "measurement",
     "content_class_id": "native-1080p-sdr",
     "settings": [{"setting_id": "qsv.q",          "value": "24", "role": "identity"},
                  {"setting_id": "qsv.preset",     "value": "4",  "role": "identity"},
                  {"setting_id": "qsv.b_strategy", "value": "-1", "role": "identity"}]}]'
{"shipped_ids":[1]}
```

A value chosen for a reason other than measurement is not a lie to be hidden — ship it as `policy`
with the reason, and the record says which it was. Note the second check named in that refusal:
the lane's throughput floor was also unmet, and a refusal always lists the others firing with it.

**A host that will not serve a lane** is recorded rather than left as a hole:

```sh
sweep exclude-route --lane tablet-1080p-sdr --host box-a-score --reason "a score runtime encodes nothing"
```

---

## 9. Reading the record

The hub's database is the record. `sweep export` renders all of it as deterministic JSON, so a change
is a diff someone reviews:

```sh
sweep export --into ../campaign/record
```

```
authored/host.json  authored/lane.json  authored/setting.json  …   the FILE tables: what you authored
sample/title.json   sample/cut.json                                the sample: what a scan and a staging found
agents/host_identity.json  agents/published.json                   what agents reported outside any run
runs/<run_id>/plan.json                                            the run, its windows, its cells and their settings
runs/<run_id>/events.jsonl                                         its life, with who wrote each event
runs/<run_id>/records/encode.json  …/score.json  …/timing.json     what came back
constants.json     openapi.json                                    calibrated constants; the API contract
```

Rows are in primary-key order and keys are sorted, so re-exporting unchanged state is an empty diff
and a changed row changes exactly one file. Per-frame metric arrays are the one thing kept out — they
live beside the store, gzipped, because they are large and nobody diffs them.

---

## 10. Where the harness stops today

Six of the record's thirteen stages have a verb behind them: `inventory`, `materialise`, the encode
stage, `viewing`, `score` and `time`, plus publish, which is a job rather than a run. The other seven
— `verify`, `screen`, `locate`, `split`, `concurrency`, `probe` and `calibrate` — exist in the schema
and have no way to run yet.

**The consequence worth knowing before you plan a campaign: the search path is not drivable.** A
search is the thing that sweeps a ladder across arms, and authoring one requires evidence that only
the screen stage produces — an admissibility verdict that the anchor opens and is monotone, and a
verdict that each arm's settings are honoured by the unit:

```
$ sweep author-search --search-id arc-av1 --content-class-id native-1080p-sdr …
REFUSING: x_arm_setting_not_honoured: an arm using a setting the unit is not MEASURED to honour on
any member of the class, e.g. ('base', 'qsv.preset') (x_search_mode_not_admissible also fires) --
screen the setting on a member of the class to HONOURED before author-search puts it in an arm
```

So `author-search`, `set-shipping-arm` and `sweep encode --search-id` are present, typed and tested,
and will refuse against a store built the way this guide builds one. Until the screen lands, the way
to measure a set of configurations is the viewing run of §6: name the cells you want, score them,
time them, read the export.

**And the numbers in a `ship` row still come from analysis the harness does not do.** It will refuse
a configuration it never encoded, and it records the provenance you claim — but ranking arms,
inverting a ladder to a target and calibrating a constant are done by reading the export. `SPEC.md`
describes those stages; they do not exist as verbs.

---

## 11. Every verb

| verb | what it does |
|---|---|
| `apply` | the fleet as one document: hosts, units and scorers created, updated or removed in one transaction |
| `add-host` | one runtime: its machine, roots, tool paths |
| `add-unit` | one encoder unit and the host it sits in |
| `add-concept` | a canonical name for a thing vendors spell differently |
| `add-setting` | one flag: its type, range, the concepts it carries, the units it applies to |
| `add-lane` | one row of what you ship: content matched, output produced, how the value is decided |
| `add-constant` | a number the transcode flow consumes, with where its value came from |
| `scope-constant` | the lanes a constant may be used by |
| `add-ladder` | the anchor values a shipped setting may take, per codec |
| `author-chain` | the production filter graph for one (lane, host, unit) |
| `add-scorer` | the intent of scoring on a host: binaries, backend, GPU, cache |
| `set-floor` | the throughput a host must reach for a lane |
| `block-host` | take a host out of service, with the fix quoted at whoever tries to use it |
| `unblock-host` | put it back |
| `pin-window` | pin a cut of a title as a window |
| `classify-cut` | record what a content check found, and why the cut is kept |
| `define-class` | the named set of windows a verdict is keyed to |
| `author-search` | a ladder sweep across arms *(needs the screen stage — §10)* |
| `set-shipping-arm` | which arm of a search ships *(same)* |
| `record-viewing` | a person's verdict on two encodes, on a named device |
| `ship` | the value a flow will run, for one (lane, host), with its provenance |
| `exclude-route` | a host that will not serve a lane, with the reason |
| `inventory` | scan a library into title rows |
| `materialise` | stage and hash the reference and source cuts |
| `encode` | plan an encode run: a search's ladder, or named cells as a viewing run |
| `score` | score a run's encodes on a host with a scorer |
| `time` | time a run's configurations through the lane's chain, on a quiet machine |
| `publish` | copy a run's encodes or some cuts to the share, hashed both ends |
| `abandon` | stop a run; the agent reads it between cells |
| `watch` | stream a run's events until it finishes |
| `status` | every run, host, queue depth, heartbeat and reported artifact |
| `check` | the store's own checks: what is wrong right now, if anything |
| `export` | the whole record as deterministic JSON |

One subcommand is not in that table because it reaches no endpoint: `sweep config`, of §1, which
reads and writes the file holding your hub and token. Everything else here is one verb, one endpoint,
and a test proves that list equal to the hub's own OpenAPI document.
