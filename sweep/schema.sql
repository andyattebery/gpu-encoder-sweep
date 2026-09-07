-- sweep/schema.sql -- THE DATA MODEL, in the form SQLite can load.
--
-- This file is the source. DATA-MODEL.md keeps the argument (THE KEY, THE SAMPLE, the stage
-- matrix, the rules, the absences); its schema blocks, diagrams, writers list and check list
-- are RENDERED from this file by `python3 sweep/model_check.py --render`, and `--check`
-- fails when they are stale. Edit here, never in the rendered blocks.
--
-- Tags on every table, read by model_check.py:
--   @group   reference | sample | measurement | decision     grouping for the doc and diagrams
--   @class   FILE | ROW                                        a human authored it | a machine observed it
--   @writer  <stage>                                           the ONE writer; FILE tables are `authored`
-- Every table carries all three. A CREATE VIEW named x_* is a CHECK: it must return ZERO rows
-- against a valid store, and the `-- @check` line above it is the one-line meaning rendered
-- into the doc. Views named v_* are derived readings, not checks.
--
-- Conventions: STRICT tables (a value has one type). Enumerations are CHECK constraints so the
-- doc can render them. Three different "step" vocabularies are deliberately three columns:
-- run.stage (a process stage), step_trace.scoring_step, lane_step.step (a flow step).

PRAGMA foreign_keys = ON;

-- ============================================================================ REFERENCE

-- @group reference
-- @class FILE
-- @writer authored
CREATE TABLE host (                                         -- a RUNTIME: an encode container, a score container, a native install; a machine may hold several
  host        TEXT PRIMARY KEY,                           -- media-01 | media-01-score | htpc-01 | eta | eta-wsl | nas-01
  machine     TEXT NOT NULL,                              -- the box; the quiet-box rule is per machine
  ssh_host    TEXT NOT NULL,
  os          TEXT NOT NULL CHECK (os IN ('linux','windows')),
  work_root   TEXT NOT NULL,
  share_root  TEXT NOT NULL,                              -- the share in this host's spelling
  local_view  TEXT,                                       -- how this runtime sees another's work root on the same machine
  ffmpeg      TEXT,                                       -- the patched build's path on this host; NULL on a host with no units (the hub's own, a scorer)
  notes       TEXT,
  blocked     TEXT                                        -- NULL = usable; otherwise THE FIX, and a run on it is refused at the moment of use
) STRICT;

-- @group reference
-- @class FILE
-- @writer authored
CREATE TABLE encoder_unit (                                 -- the measurement key's hardware half
  encoder_unit_id TEXT PRIMARY KEY,
  vendor      TEXT NOT NULL CHECK (vendor IN ('nvidia','amd','intel')),
  card        TEXT NOT NULL,
  driver      TEXT NOT NULL,                               -- a FACTOR: 25.2.3 -> 26.2.2 moved bytes 14/14
  frontend    TEXT NOT NULL CHECK (frontend IN ('nvenc','vaapi','qsv')),
  codec       TEXT NOT NULL CHECK (codec IN ('hevc','av1')),
  UNIQUE (vendor, card, driver, frontend, codec)
) STRICT;

-- @group reference
-- @class FILE
-- @writer authored
CREATE TABLE host_unit (                                    -- which units are in which box; routing reads it, the key does not
  host            TEXT NOT NULL REFERENCES host,
  encoder_unit_id TEXT NOT NULL REFERENCES encoder_unit,
  device          TEXT NOT NULL CHECK (device NOT LIKE '%renderD%'),   -- by PCI slot or a stable id; never a render-node number, which inverted twice
  PRIMARY KEY (host, encoder_unit_id)
) STRICT;

-- @group reference
-- @class FILE
-- @writer authored
CREATE TABLE scorer (                                       -- the INTENT of scoring on a host: argv, backend, card, cache; the build that ran is run.scorer_build, from the artifact the agent reports
  host           TEXT PRIMARY KEY REFERENCES host,
  ffvship        TEXT NOT NULL,                             -- argv, compact JSON
  score_ffmpeg   TEXT NOT NULL,                             -- argv, compact JSON
  metric_backend TEXT NOT NULL CHECK (metric_backend IN ('libvmaf','libvmaf_cuda')),
  gpu_id         INTEGER NOT NULL,
  cache_dir      TEXT NOT NULL
) STRICT;

-- @group reference
-- @class FILE
-- @writer authored
CREATE TABLE canonical_concept (                            -- one concept, several vendor spellings
  canonical_id TEXT PRIMARY KEY,                            -- quality_anchor | rate_control_mode | preset ...
  description  TEXT NOT NULL
) STRICT;

-- @group reference
-- @class FILE
-- @writer authored
CREATE TABLE setting (
  setting_id  TEXT PRIMARY KEY,                             -- e.g. qsv.b_strategy
  flag        TEXT NOT NULL,                                -- the ffmpeg spelling: -b_strategy
  frontend    TEXT CHECK (frontend IN ('nvenc','vaapi','qsv')),   -- NULL when is_generic
  kind        TEXT NOT NULL CHECK (kind IN ('quality_anchor','mode_selector','ordinal','option')),
  subsystem   TEXT NOT NULL CHECK (subsystem IN ('rate_control','frame_types','tiles','lookahead','plumbing','other')),
  value_type  TEXT NOT NULL CHECK (value_type IN ('int','real','enum','bool','text')),
  range_lo    REAL,
  range_hi    REAL,
  is_generic  INTEGER NOT NULL CHECK (is_generic IN (0,1)), -- a generic ffmpeg option, absent from every private dump
  notes       TEXT,
  CHECK ((frontend IS NULL) = (is_generic = 1))
) STRICT;

-- @group reference
-- @class FILE
-- @writer authored
CREATE TABLE setting_enum_value (                           -- a cell_setting value must be one of these
  setting_id  TEXT NOT NULL REFERENCES setting,
  value       TEXT NOT NULL,
  PRIMARY KEY (setting_id, value)
) STRICT;

-- @group reference
-- @class FILE
-- @writer authored
CREATE TABLE setting_role (                                 -- ONE flag may carry TWO concepts: qsv -q:v selects CQP AND is the anchor
  setting_id   TEXT NOT NULL REFERENCES setting,
  canonical_id TEXT NOT NULL REFERENCES canonical_concept,
  PRIMARY KEY (setting_id, canonical_id)
) STRICT;

-- @group reference
-- @class FILE
-- @writer authored
CREATE TABLE setting_scope (
  setting_id          TEXT NOT NULL REFERENCES setting,
  encoder_unit_id     TEXT NOT NULL REFERENCES encoder_unit,
  applies             INTEGER NOT NULL CHECK (applies IN (0,1)),
  default_value       TEXT,
  default_is_measured INTEGER NOT NULL CHECK (default_is_measured IN (0,1)),   -- absent is not "off"
  PRIMARY KEY (setting_id, encoder_unit_id)
) STRICT;

-- @group reference
-- @class FILE
-- @writer authored
CREATE TABLE constant (                                     -- the numbers the flow's per-title logic consumes; the harness produces them and runs none of that logic
  name        TEXT PRIMARY KEY,                             -- CEILING | MARGIN | HEADROOM | BOUND | RUNG_FACTOR | HOST_THRESHOLD
  value       REAL,                                        -- derived and policy: authored here. measured: in constant_value, from the calibrate stage
  unit        TEXT NOT NULL,
  provenance  TEXT NOT NULL CHECK (provenance IN ('measured','derived','policy')),   -- policy: a chosen value with its reason -- MARGIN, the skip threshold
  inputs_json TEXT,                                         -- derived: the inputs, so the precision is honest
  precision   TEXT,                                         -- e.g. ±~10%
  reason      TEXT,                                         -- policy: why this value, and what bounds it
  cites_json  TEXT,
  CHECK (provenance <> 'derived' OR (inputs_json IS NOT NULL AND precision IS NOT NULL)),
  CHECK (provenance <> 'policy' OR reason IS NOT NULL),
  CHECK ((provenance IN ('derived','policy')) = (value IS NOT NULL))
) STRICT;

-- @group reference
-- @class FILE
-- @writer authored
CREATE TABLE lane (                                         -- TDARR-TRANSCODE-PLAN.md ## Coverage, one row per lane
  lane                 TEXT PRIMARY KEY,
  codec                TEXT NOT NULL CHECK (codec IN ('hevc','av1')),
  decision_rule        TEXT NOT NULL CHECK (decision_rule IN ('cap','incumbent','target')),  -- HOW the shipped value is decided; Stage 10 inverts on it
  input_width_min      INTEGER,                             -- NULL = any; Coverage's "> 1920" is min 1921
  input_width_max      INTEGER,                             -- NULL = any; Coverage's "<= 1920" is max 1920
  input_dynamic_range  TEXT NOT NULL CHECK (input_dynamic_range IN ('sdr','hdr')),
  output_resolution    TEXT NOT NULL,                       -- 1080p | native
  output_dynamic_range TEXT NOT NULL CHECK (output_dynamic_range IN ('sdr','hdr')),
  hdr_handling         TEXT NOT NULL CHECK (hdr_handling IN ('n/a','tonemapping','passthrough')),
  audio                TEXT NOT NULL,
  subtitles            TEXT NOT NULL,
  score_target         REAL,                                -- only a target-bound lane has one
  score_height         INTEGER NOT NULL,                    -- the device's 16:9 panel height; every lane scores somewhere
  bitrate_cap_binds    TEXT NOT NULL CHECK (bitrate_cap_binds IN ('never','rarely','always')),
  bitrate_cap_constant TEXT REFERENCES constant,            -- the CEILING, where a cap exists
  has_content          INTEGER NOT NULL CHECK (has_content IN (0,1)),
  min_content_rate     REAL,                                -- the deadline: content minutes per wall minute a host must reach; NULL = report only
  CHECK ((decision_rule = 'target') = (score_target IS NOT NULL)),
  CHECK (decision_rule <> 'cap' OR bitrate_cap_constant IS NOT NULL),
  CHECK ((bitrate_cap_binds = 'never') = (bitrate_cap_constant IS NULL))
) STRICT;

-- @group reference
-- @class FILE
-- @writer authored
CREATE TABLE lane_step (                                    -- the flow steps a lane has; a shipped row must name one
  lane  TEXT NOT NULL REFERENCES lane,
  step  TEXT NOT NULL CHECK (step IN ('probe','remux','quality-target-encode','bitrate-target-encode')),
  PRIMARY KEY (lane, step)
) STRICT;

-- @group reference
-- @class FILE
-- @writer authored
CREATE TABLE constant_scope (                               -- the lanes a constant admits; outside it is a refusal
  name  TEXT NOT NULL REFERENCES constant,
  lane  TEXT NOT NULL REFERENCES lane,
  PRIMARY KEY (name, lane)
) STRICT;

-- @group reference
-- @class FILE
-- @writer authored
CREATE TABLE ladder (                                       -- ONE ladder per codec, never per host
  ladder_id TEXT PRIMARY KEY,
  codec     TEXT NOT NULL UNIQUE CHECK (codec IN ('hevc','av1'))
) STRICT;

-- @group reference
-- @class FILE
-- @writer authored
CREATE TABLE ladder_rung (                                  -- a shipped quality anchor must be one of these
  ladder_id TEXT NOT NULL REFERENCES ladder,
  rung      INTEGER NOT NULL,
  PRIMARY KEY (ladder_id, rung)
) STRICT;

-- @group reference
-- @class FILE
-- @writer authored
CREATE TABLE chain (                                        -- the production filter graph per (lane, host, unit): AUTHORED before the sample -- the reference cut is built through it; Stage 7 times it; Stage 11 ships the same row
  lane            TEXT NOT NULL REFERENCES lane,
  host            TEXT NOT NULL REFERENCES host,
  encoder_unit_id TEXT NOT NULL REFERENCES encoder_unit,    -- the filter graph is per API: a box with two frontends has two chains for one lane
  vf_template     TEXT NOT NULL,
  notes_ref       TEXT,
  PRIMARY KEY (lane, host, encoder_unit_id),
  FOREIGN KEY (host, encoder_unit_id) REFERENCES host_unit (host, encoder_unit_id)   -- the unit is in that box
) STRICT;

-- ============================================================================ SAMPLE

-- @group sample
-- @class ROW
-- @writer inventory
CREATE TABLE title (                                        -- the POPULATION: one row per library file, from a scan
  title_id        TEXT PRIMARY KEY,
  path            TEXT NOT NULL UNIQUE,
  library         TEXT NOT NULL,
  width           INTEGER NOT NULL,
  height          INTEGER NOT NULL,
  dynamic_range   TEXT NOT NULL CHECK (dynamic_range IN ('sdr','hdr10','hlg','dv')),
  dv_profile      INTEGER,
  video_codec     TEXT NOT NULL,
  field_order     TEXT NOT NULL,
  fps             REAL NOT NULL,
  bit_depth       INTEGER NOT NULL,
  bitrate_kbps    REAL NOT NULL,
  bpp             REAL NOT NULL,                            -- bits per pixel per frame, so resolutions compare
  source_type     TEXT NOT NULL CHECK (source_type IN ('remux','bluray','web','other')),
  audio_layout    TEXT,
  subtitle_layout TEXT,
  scanned_at      TEXT NOT NULL,
  CHECK ((dynamic_range = 'dv') = (dv_profile IS NOT NULL))
) STRICT;

-- @group sample
-- @class FILE
-- @writer authored
CREATE TABLE window (                                       -- a cut of a title: (title, ss, t) and nothing else
  window_id       TEXT PRIMARY KEY,
  title_id        TEXT NOT NULL REFERENCES title,
  ss              REAL NOT NULL,
  t               REAL NOT NULL,
  character       TEXT,                                     -- what the window is FOR; judgement, written down
  origin          TEXT NOT NULL CHECK (origin IN ('pinned','generated')),
  selected_by     TEXT,                                     -- the selection policy and its parameters
  selection_score REAL,
  notes           TEXT
) STRICT;

-- @group sample
-- @class ROW
-- @writer materialise
CREATE TABLE reference_set (                                -- one reference pixel set: one geometry, built once, on one host
  reference_set_id TEXT PRIMARY KEY,
  geometry    TEXT NOT NULL,                                -- 1920x1080
  pix_fmt     TEXT NOT NULL,                                -- p010le
  built_on    TEXT NOT NULL REFERENCES host,
  built_with  TEXT NOT NULL,                                -- ffmpeg build and sha
  built_at    TEXT NOT NULL
) STRICT;

-- @group sample
-- @class FILE
-- @writer authored
CREATE TABLE content_class (                                -- a named, authored set of windows; the key's content half
  content_class_id TEXT PRIMARY KEY,
  name             TEXT NOT NULL UNIQUE,
  reference_set_id TEXT NOT NULL REFERENCES reference_set,  -- bound to exactly ONE reference pixel set
  description      TEXT
) STRICT;

-- @group sample
-- @class FILE
-- @writer authored
CREATE TABLE content_class_lane (                           -- the lanes a class is sampled FOR
  content_class_id TEXT NOT NULL REFERENCES content_class,
  lane             TEXT NOT NULL REFERENCES lane,
  PRIMARY KEY (content_class_id, lane)
) STRICT;

-- @group sample
-- @class FILE
-- @writer authored
CREATE TABLE content_class_member (
  content_class_id TEXT NOT NULL REFERENCES content_class,
  window_id        TEXT NOT NULL REFERENCES window,
  PRIMARY KEY (content_class_id, window_id)
) STRICT;

-- @group sample
-- @class FILE
-- @writer authored
CREATE TABLE content_class_stratum (                        -- THE FRAME: how the class was picked
  content_class_id TEXT NOT NULL REFERENCES content_class,
  stratum          TEXT NOT NULL,
  kind             TEXT NOT NULL CHECK (kind IN ('inventory','quantile','character')),
  definition       TEXT NOT NULL,                           -- inventory/quantile: a SQL boolean over title t.*; character: a name
  min_windows      INTEGER NOT NULL DEFAULT 1,
  share_estimate   REAL,                                    -- character strata only: judgement, and says so
  PRIMARY KEY (content_class_id, stratum),
  CHECK ((kind = 'character') = (share_estimate IS NOT NULL))
) STRICT;

-- @group sample
-- @class ROW
-- @writer materialise
CREATE TABLE cut (                                          -- one window's file in one reference set
  cut_id           TEXT PRIMARY KEY,
  reference_set_id TEXT NOT NULL REFERENCES reference_set,
  window_id        TEXT NOT NULL REFERENCES window,
  kind             TEXT NOT NULL CHECK (kind IN ('reference','source')),
  chain_lane       TEXT,                                    -- reference cuts: the chain that built it, by its three-part key
  chain_host       TEXT,
  chain_unit       TEXT,
  content_sha      TEXT NOT NULL,                           -- of decoded FRAMES, never the container
  bytes            INTEGER NOT NULL,
  frames           INTEGER NOT NULL,
  tags_pinned      TEXT,
  UNIQUE (reference_set_id, window_id, kind),
  FOREIGN KEY (chain_lane, chain_host, chain_unit) REFERENCES chain (lane, host, encoder_unit_id),
  CONSTRAINT cut_reference_names_chain CHECK ((kind = 'reference') = (chain_lane IS NOT NULL)   -- all three parts or none: SQLite skips a composite FK with a NULL in it
    AND (chain_lane IS NULL) = (chain_host IS NULL) AND (chain_lane IS NULL) = (chain_unit IS NULL))
) STRICT;

-- @group sample
-- @class ROW
-- @writer verify
CREATE TABLE cut_check (                                    -- a content check, not a checksum
  cut_id     TEXT NOT NULL REFERENCES cut,
  check_name TEXT NOT NULL,
  result     TEXT NOT NULL CHECK (result IN ('pass','fail','classified')),
  reason     TEXT,
  checked_at TEXT NOT NULL,
  PRIMARY KEY (cut_id, check_name, checked_at),
  CHECK (result <> 'classified' OR reason IS NOT NULL)     -- a classification needs a reason
) STRICT;

-- ============================================================================ MEASUREMENT

-- @group measurement
-- @class ROW
-- @writer orchestrate
CREATE TABLE run (                                          -- one invocation
  run_id           TEXT PRIMARY KEY,
  encoder_unit_id  TEXT REFERENCES encoder_unit,            -- NULL only for a unit-less stage: inventory, verify
  content_class_id TEXT REFERENCES content_class,           -- NULL for inventory, materialise, verify and the probe (a library-title run)
  search_id        TEXT REFERENCES search,                  -- the spec this run executes; NULL for screen, viewing, probe, calibrate
  parent_run_id    TEXT REFERENCES run,                     -- a score run scores its parent's cells and has none of its own; NULL on every other stage
  host             TEXT NOT NULL REFERENCES host,           -- where it RAN; not part of the measurement key
  node_label       TEXT NOT NULL,                           -- a distinct ledger identity per card in a multi-card box
  stage            TEXT NOT NULL CHECK (stage IN ('inventory','materialise','verify','screen','locate','encode','score','time','split','concurrency','viewing','probe','calibrate')),
  artifact         TEXT NOT NULL,                           -- what the plan was built for: the image digest, or the package version and sha; the agent reports it
  ffmpeg_build     TEXT NOT NULL,
  ffmpeg_sha       TEXT NOT NULL,
  scorer_build     TEXT,
  ffvship_version  TEXT,
  metric_backend   TEXT,
  harness_version  TEXT NOT NULL,
  started_at       TEXT NOT NULL,
  finished_at      TEXT,
  state            TEXT NOT NULL DEFAULT 'planned' CHECK (state IN ('planned','launched','running','complete','failed','abandoned')),
  fetched_at       TEXT,                                    -- when the product reached the store
  verified_at      TEXT,                                    -- when count and heights were checked against the plan
  CHECK (state <> 'complete' OR verified_at IS NOT NULL),
  CONSTRAINT run_unit_by_stage CHECK ((stage IN ('inventory','verify')) = (encoder_unit_id IS NULL)),  -- a scan and a content check use no encoder; every other stage names one
  CONSTRAINT run_score_has_parent CHECK ((stage = 'score') = (parent_run_id IS NOT NULL))               -- a score run scores a parent; nothing else has one
) STRICT;

-- @group measurement
-- @class ROW
-- @writer orchestrate
CREATE TABLE run_event (                                    -- append-only transitions: the log the waiter reads, instead of a log file
  run_id TEXT NOT NULL REFERENCES run,
  at     TEXT NOT NULL,
  state  TEXT NOT NULL CHECK (state IN ('planned','launched','running','complete','failed','abandoned')),
  detail TEXT,                                              -- the claim and the artifact the agent reported, the failure
  by     TEXT NOT NULL CHECK (by IN ('hub','agent')),       -- who wrote it: the hub plans, launches at claim and completes; the agent reports
  PRIMARY KEY (run_id, at, state)
) STRICT;

-- @group measurement
-- @class ROW
-- @writer orchestrate
CREATE TABLE run_window (                                   -- what the run actually COVERED
  run_id    TEXT NOT NULL REFERENCES run,
  window_id TEXT NOT NULL REFERENCES window,
  PRIMARY KEY (run_id, window_id)
) STRICT;

-- @group measurement
-- @class ROW
-- @writer orchestrate
CREATE TABLE cell (                                         -- one encode: the common core of every encoding stage
  cell_key  TEXT PRIMARY KEY,                               -- content-addressed: settings, cut sha, tool versions
  run_id    TEXT NOT NULL REFERENCES run,
  window_id TEXT NOT NULL REFERENCES window,
  cut_kind  TEXT NOT NULL CHECK (cut_kind IN ('reference','source','library'))
) STRICT;

-- @group measurement
-- @class ROW
-- @writer orchestrate
CREATE TABLE cell_setting (                                 -- ONE-TO-MANY: the settings a cell was encoded with
  cell_key   TEXT NOT NULL REFERENCES cell,
  setting_id TEXT NOT NULL REFERENCES setting,
  value      TEXT NOT NULL,
  role       TEXT NOT NULL CHECK (role IN ('identity','computed','default_resolved')),
  PRIMARY KEY (cell_key, setting_id)
) STRICT;

-- @group measurement
-- @class ROW
-- @writer encode core
CREATE TABLE encode (
  cell_key     TEXT PRIMARY KEY REFERENCES cell,
  bytes        INTEGER NOT NULL,
  bitrate_kbps REAL NOT NULL,
  frames       INTEGER NOT NULL,
  duration_s   REAL NOT NULL,
  decode_path  TEXT NOT NULL CHECK (decode_path IN ('hardware','software')),
  kept         INTEGER NOT NULL CHECK (kept IN (0,1))      -- most stages discard; gone and never-made must differ
) STRICT;

-- @group measurement
-- @class ROW
-- @writer encode core
CREATE TABLE cell_failure (                                 -- a planned cell that did not encode: STDERR, never the exit status alone
  cell_key TEXT PRIMARY KEY REFERENCES cell,
  at       TEXT NOT NULL,
  stderr   TEXT NOT NULL,
  rc       INTEGER                                          -- recorded, never trusted
) STRICT;

-- @group measurement
-- @class ROW
-- @writer score
CREATE TABLE score (                                        -- ONE-TO-MANY: height x metric x statistic, under the score run that produced it
  run_id    TEXT NOT NULL REFERENCES run,                   -- the score run, never the cell's encode run
  cell_key  TEXT NOT NULL REFERENCES cell,
  height    INTEGER NOT NULL,                               -- a score without its height is not a number
  metric    TEXT NOT NULL CHECK (metric IN ('ssimulacra2','butteraugli','vmaf','cambi','psnr_y','float_ssim')),
  statistic TEXT NOT NULL CHECK (statistic IN ('mean','p5','min','max')),
  value     REAL NOT NULL,
  recipe    TEXT NOT NULL,                                 -- the named scoring recipe (SPEC.md, S1): scaler, options, pooling
  scorer_build TEXT NOT NULL,                              -- the binaries: FFVship version and the ffmpeg build sha
  PRIMARY KEY (run_id, cell_key, height, metric, statistic, recipe, scorer_build)
) STRICT;

-- @group measurement
-- @class ROW
-- @writer time
CREATE TABLE timing (                                       -- ONE-TO-MANY: workers x repeat; samples kept
  cell_key        TEXT NOT NULL REFERENCES cell,
  workers         INTEGER NOT NULL,
  repeat_index    INTEGER NOT NULL,
  fps             REAL NOT NULL,
  wall_s          REAL NOT NULL,
  decode_path     TEXT NOT NULL CHECK (decode_path IN ('hardware','software')),   -- MEASURED; speed partitions on it
  is_warmup       INTEGER NOT NULL CHECK (is_warmup IN (0,1)),                    -- flagged, never silently dropped
  noise_floor_pct REAL,
  leg             TEXT NOT NULL DEFAULT 'full' CHECK (leg IN ('full','decode','decode_filters')),  -- the split: a truncated chain
  PRIMARY KEY (cell_key, workers, repeat_index, leg)
) STRICT;

-- @group measurement
-- @class ROW
-- @writer score
CREATE TABLE step_trace (                                   -- how long each scoring leg took, under the score run
  run_id       TEXT NOT NULL REFERENCES run,
  cell_key     TEXT NOT NULL REFERENCES cell,
  height       INTEGER NOT NULL,
  scoring_step TEXT NOT NULL CHECK (scoring_step IN ('rescale_ref','rescale_enc','ssimu2','butteraugli','libvmaf')),
  seconds      REAL NOT NULL,
  cores_busy   REAL,
  gpu_mean     REAL,
  gpu_max      REAL,
  PRIMARY KEY (run_id, cell_key, height, scoring_step)
) STRICT;

-- @group measurement
-- @class ROW
-- @writer equivalence
CREATE TABLE scorer_equivalence (                           -- two score runs over the same cells, compared: exact or not, per metric and statistic
  run_a         TEXT NOT NULL REFERENCES run,
  run_b         TEXT NOT NULL REFERENCES run,
  metric        TEXT NOT NULL CHECK (metric IN ('ssimulacra2','butteraugli','vmaf','cambi','psnr_y','float_ssim')),
  statistic     TEXT NOT NULL CHECK (statistic IN ('mean','p5','min','max')),
  cells         INTEGER NOT NULL,                           -- how many cells both runs scored
  max_abs_delta REAL NOT NULL,
  exact         INTEGER NOT NULL CHECK (exact IN (0,1)),    -- bit-identical across the two scorers; a search is split across scorers only when exact
  PRIMARY KEY (run_a, run_b, metric, statistic)
) STRICT;

-- @group measurement
-- @class ROW
-- @writer screen
CREATE TABLE setting_verdict (                              -- PER WINDOW; the unit-level reading is v_setting_unit_reading
  verdict_id      INTEGER PRIMARY KEY,
  encoder_unit_id TEXT NOT NULL REFERENCES encoder_unit,
  setting_id      TEXT NOT NULL REFERENCES setting,
  window_id       TEXT NOT NULL REFERENCES window,
  verdict         TEXT NOT NULL CHECK (verdict IN ('HONOURED','INERT','PARTIAL','REJECTED','BASE_FAILED','EXCLUDED')),
  magnitude_pct   REAL,
  base_setting_id TEXT REFERENCES setting,                  -- the prerequisite it was taken under
  base_value      TEXT,
  noise_floor_pct REAL,
  reason          TEXT,                                     -- EXCLUDED needs one
  UNIQUE (encoder_unit_id, setting_id, window_id, base_setting_id, base_value),
  CHECK (verdict <> 'EXCLUDED' OR reason IS NOT NULL)
) STRICT;

-- @group measurement
-- @class ROW
-- @writer screen
CREATE TABLE setting_verdict_cell (                         -- the encodes a verdict summarises; 5 repeats for the floor
  verdict_id INTEGER NOT NULL REFERENCES setting_verdict,
  cell_key   TEXT NOT NULL REFERENCES cell,
  PRIMARY KEY (verdict_id, cell_key)
) STRICT;


-- @group measurement
-- @class FILE
-- @writer authored
CREATE TABLE search (                                       -- the spec: what a search measures, on what, at what height
  search_id         TEXT PRIMARY KEY,
  content_class_id  TEXT NOT NULL REFERENCES content_class,
  encoder_unit_id   TEXT NOT NULL REFERENCES encoder_unit,
  anchor_setting_id TEXT NOT NULL REFERENCES setting,       -- the quality anchor every ladder is placed on
  score_height      INTEGER NOT NULL,                       -- decided HERE, before anything is scored
  notes             TEXT,
  shipping_arm_id   TEXT REFERENCES arm                     -- the arm that ships: the base, unless a candidate beat it on efficiency and met the deadline
) STRICT;

-- @group measurement
-- @class FILE
-- @writer authored
CREATE TABLE arm (                                          -- a candidate: one set of identity settings
  arm_id    TEXT PRIMARY KEY,
  search_id TEXT NOT NULL REFERENCES search,
  name      TEXT NOT NULL,
  role      TEXT NOT NULL CHECK (role IN ('base','candidate','incumbent')),   -- exactly one base (the mandatory path); candidates only when the screen earned a search; one incumbent where the rule needs it
  accepted_by_viewing INTEGER REFERENCES viewing_verdict,   -- an incumbent arm names the acceptance viewing that says it is acceptable
  anchor_value TEXT,                                        -- the incumbent is PINNED at the anchor it ships and its cell may be the base arm's; the base and the candidates sweep the anchor
  UNIQUE (search_id, name),
  CHECK ((role = 'incumbent') = (anchor_value IS NOT NULL))
) STRICT;

-- @group measurement
-- @class FILE
-- @writer authored
CREATE TABLE search_coarse_rung (                           -- the ONE shared ladder the locate pass encodes
  search_id TEXT NOT NULL REFERENCES search,
  rung      INTEGER NOT NULL,
  PRIMARY KEY (search_id, rung)
) STRICT;

-- @group measurement
-- @class FILE
-- @writer authored
CREATE TABLE arm_setting (                                  -- the same shape as cell_setting: a cell belongs to the arm it equals
  arm_id     TEXT NOT NULL REFERENCES arm,
  setting_id TEXT NOT NULL REFERENCES setting,
  value      TEXT NOT NULL,
  PRIMARY KEY (arm_id, setting_id)
) STRICT;

-- @group measurement
-- @class FILE
-- @writer authored
CREATE TABLE search_target (                                -- the fixed points the column is read at
  search_id TEXT NOT NULL REFERENCES search,
  metric    TEXT NOT NULL CHECK (metric IN ('ssimulacra2','butteraugli','vmaf','cambi','psnr_y','float_ssim')),
  statistic TEXT NOT NULL CHECK (statistic IN ('mean','p5','min','max')),
  target    REAL NOT NULL,
  viewing_id INTEGER REFERENCES viewing_verdict,           -- a target-bound lane's target names the acceptance viewing it came from
  PRIMARY KEY (search_id, metric, statistic, target)
) STRICT;

-- @group measurement
-- @class ROW
-- @writer derive ladders
CREATE TABLE arm_ladder_rung (                              -- one ladder per (arm, window), placed from the LOCATE run it names
  run_id    TEXT NOT NULL REFERENCES run,                    -- the locate run; never written into the spec
  arm_id    TEXT NOT NULL REFERENCES arm,
  window_id TEXT NOT NULL REFERENCES window,
  rung      INTEGER NOT NULL,
  PRIMARY KEY (run_id, arm_id, window_id, rung)
) STRICT;

-- @group measurement
-- @class ROW
-- @writer calibrate
CREATE TABLE constant_value (                               -- a measured constant's value, from the run it was computed on
  name        TEXT NOT NULL REFERENCES constant,
  run_id      TEXT NOT NULL REFERENCES run,
  value       REAL NOT NULL,
  computed_at TEXT NOT NULL,
  PRIMARY KEY (name, run_id)
) STRICT;

-- ============================================================================ DECISION

-- @group decision
-- @class FILE
-- @writer ship
CREATE TABLE shipped (                                      -- ONE value per (lane, host, step); may override a measurement
  shipped_id       INTEGER PRIMARY KEY,
  lane             TEXT NOT NULL REFERENCES lane,
  host             TEXT NOT NULL REFERENCES host,
  step             TEXT NOT NULL,
  encoder_unit_id  TEXT REFERENCES encoder_unit,            -- NULL for remux
  provenance       TEXT NOT NULL CHECK (provenance IN ('measured','derived','no-content','fixed')),   -- fixed: a remux, no encoder decision
  decided_by       TEXT NOT NULL CHECK (decided_by IN ('measurement','policy')),
  reason           TEXT,
  content_class_id TEXT REFERENCES content_class,           -- the class the value was MEASURED on
  workers          INTEGER,
  evidence_query   TEXT,                                    -- executable, not a filename
  cites_json       TEXT,
  UNIQUE (lane, host, step),
  FOREIGN KEY (lane, step) REFERENCES lane_step (lane, step),
  CHECK (provenance <> 'measured' OR content_class_id IS NOT NULL),
  CHECK (decided_by <> 'policy' OR reason IS NOT NULL),
  CHECK ((step = 'remux') = (provenance = 'fixed'))
) STRICT;

-- @group decision
-- @class FILE
-- @writer ship
CREATE TABLE shipped_setting (                              -- the same shape as cell_setting, so "did we measure this?" is a set comparison
  shipped_id    INTEGER NOT NULL REFERENCES shipped,
  setting_id    TEXT NOT NULL REFERENCES setting,
  value         TEXT NOT NULL,
  role          TEXT NOT NULL CHECK (role IN ('identity','computed','default_resolved')),
  from_constant TEXT REFERENCES constant,                   -- a computed value names the constant it came from
  PRIMARY KEY (shipped_id, setting_id),
  CHECK (role = 'computed' OR from_constant IS NULL)
) STRICT;


-- @group decision
-- @class FILE
-- @writer ship
CREATE TABLE routing_exclusion (                            -- a host a lane is deliberately NOT routed to, with the reason
  lane   TEXT NOT NULL REFERENCES lane,
  host   TEXT NOT NULL REFERENCES host,
  reason TEXT NOT NULL,
  PRIMARY KEY (lane, host)
) STRICT;

-- @group decision
-- @class FILE
-- @writer viewing
CREATE TABLE viewing_verdict (                              -- a person's verdict on the device: the one measurement whose instrument is eyes
  viewing_id INTEGER PRIMARY KEY,
  kind      TEXT NOT NULL CHECK (kind IN ('pair','acceptance')),   -- a against b, or: is a acceptable for this lane
  lane      TEXT REFERENCES lane,                           -- acceptance: the use the judgement is for (the kids watch in daylight)
  window_id TEXT NOT NULL REFERENCES window,
  cell_a    TEXT NOT NULL REFERENCES cell,
  cell_b    TEXT REFERENCES cell,                           -- pair only
  device    TEXT NOT NULL,
  viewer    TEXT NOT NULL,
  verdict   TEXT NOT NULL CHECK (verdict IN ('a','b','same','unsure','acceptable','not_acceptable')),
  notes     TEXT,
  viewed_at TEXT NOT NULL,
  CHECK ((kind = 'pair') = (cell_b IS NOT NULL)),
  CHECK ((kind = 'acceptance') = (lane IS NOT NULL)),
  CHECK ((kind = 'pair' AND verdict IN ('a','b','same','unsure'))
      OR (kind = 'acceptance' AND verdict IN ('acceptable','not_acceptable','unsure')))
) STRICT;

-- ============================================================================ DERIVED READINGS (v_*)

-- a lane's population: every title its predicates admit. Flow membership (which titles a
-- flow carries) is the user's routing and narrows this; the view is the upper bound.
CREATE VIEW v_lane_population AS
  SELECT l.lane, t.title_id
    FROM lane l JOIN title t
      ON (l.input_width_min IS NULL OR t.width >= l.input_width_min)
     AND (l.input_width_max IS NULL OR t.width <= l.input_width_max)
     AND ((l.input_dynamic_range = 'sdr' AND t.dynamic_range = 'sdr')
       OR (l.input_dynamic_range = 'hdr' AND t.dynamic_range <> 'sdr'));

-- how many members of a class have a title in each served lane's population: "the row says how many"
CREATE VIEW v_class_lane_representation AS
  SELECT cl.content_class_id, cl.lane,
         (SELECT count(*) FROM content_class_member m WHERE m.content_class_id = cl.content_class_id) AS members,
         (SELECT count(*) FROM content_class_member m JOIN window w ON w.window_id = m.window_id
            JOIN v_lane_population p ON p.title_id = w.title_id AND p.lane = cl.lane
           WHERE m.content_class_id = cl.content_class_id) AS represented
    FROM content_class_lane cl;

-- the unit-level screen reading, DERIVED from per-window verdicts over the class:
-- HONOURED if any member moved; INERT only if every member was screened and none moved.
CREATE VIEW v_setting_unit_reading AS
  SELECT v.encoder_unit_id, v.setting_id, m.content_class_id,
         (SELECT count(*) FROM content_class_member mm WHERE mm.content_class_id = m.content_class_id) AS members,
         count(DISTINCT v.window_id) AS screened,
         sum(v.verdict = 'HONOURED') AS honoured_n,
         sum(v.verdict = 'INERT') AS inert_n,
         CASE WHEN sum(v.verdict = 'HONOURED') > 0 THEN 'HONOURED'
              WHEN count(DISTINCT v.window_id) =
                   (SELECT count(*) FROM content_class_member mm WHERE mm.content_class_id = m.content_class_id)
                   AND sum(v.verdict = 'INERT') = count(v.verdict) THEN 'INERT'
              ELSE 'INCOMPLETE ' || count(DISTINCT v.window_id) || ' of ' ||
                   (SELECT count(*) FROM content_class_member mm WHERE mm.content_class_id = m.content_class_id)
         END AS reading
    FROM setting_verdict v
    JOIN content_class_member m ON m.window_id = v.window_id
   GROUP BY v.encoder_unit_id, v.setting_id, m.content_class_id;

-- a constant's current value: authored for derived and policy, the latest calibrated one for measured
CREATE VIEW v_constant_current AS
  SELECT c.name, c.unit, c.provenance,
         CASE WHEN c.provenance IN ('derived','policy') THEN c.value
              ELSE (SELECT v.value FROM constant_value v WHERE v.name = c.name ORDER BY v.computed_at DESC LIMIT 1)
         END AS value
    FROM constant c;

-- a cell's state, DERIVED from its rows: never stored, so it cannot disagree with them
CREATE VIEW v_cell_state AS
  SELECT c.cell_key, c.run_id,
         CASE WHEN EXISTS (SELECT 1 FROM cell_failure f WHERE f.cell_key = c.cell_key) THEN 'failed'
              WHEN EXISTS (SELECT 1 FROM score s WHERE s.cell_key = c.cell_key) THEN 'scored'
              WHEN EXISTS (SELECT 1 FROM timing ti WHERE ti.cell_key = c.cell_key) THEN 'timed'
              WHEN EXISTS (SELECT 1 FROM encode e WHERE e.cell_key = c.cell_key) THEN 'encoded'
              ELSE 'planned' END AS state
    FROM cell c;

-- a run's progress: the plan is its cells, so the expected count is never typed; a score run's cells are its parent's,
-- and it has scored the ones that carry a score row under ITS run_id
CREATE VIEW v_run_progress AS
  SELECT r.run_id, r.stage, r.state,
         count(cs.cell_key) AS planned_total,
         sum(cs.state = 'planned') AS still_planned,
         sum(cs.state = 'encoded') AS encoded,
         sum(CASE WHEN r.stage = 'score'
                  THEN EXISTS (SELECT 1 FROM score sc WHERE sc.cell_key = cs.cell_key AND sc.run_id = r.run_id)
                  ELSE cs.state = 'scored' END) AS scored,
         sum(cs.state = 'timed') AS timed,
         sum(cs.state = 'failed') AS failed
    FROM run r LEFT JOIN v_cell_state cs ON cs.run_id = coalesce(r.parent_run_id, r.run_id)
   GROUP BY r.run_id;

-- ============================================================================ CHECKS (x_*): each must return ZERO rows

-- @check a lane marked has_content whose population is EMPTY (the other direction is judgement: flow membership narrows)
-- @fix add-lane with input bounds a scanned title fits, or with has_content 0; an empty population is not a lane with content
CREATE VIEW x_has_content_but_empty AS
  SELECT l.lane FROM lane l
   WHERE l.has_content = 1
     AND NOT EXISTS (SELECT 1 FROM v_lane_population p WHERE p.lane = l.lane);

-- @check a shipped quality anchor that is not a rung on its lane's codec's ladder
-- @fix ship an anchor value that is a rung of the lane's codec ladder (add-ladder lists them); a value off the ladder was never encoded on every member
CREATE VIEW x_shipped_not_a_rung AS
  SELECT s.shipped_id, s.lane, ss.setting_id, ss.value
    FROM shipped s
    JOIN shipped_setting ss ON ss.shipped_id = s.shipped_id
    JOIN setting st ON st.setting_id = ss.setting_id AND st.kind = 'quality_anchor'
    JOIN encoder_unit u ON u.encoder_unit_id = s.encoder_unit_id
    JOIN ladder ld ON ld.codec = u.codec
   WHERE NOT EXISTS (SELECT 1 FROM ladder_rung r
                      WHERE r.ladder_id = ld.ladder_id AND r.rung = CAST(ss.value AS INTEGER));

-- @check a `measured` value whose evidence class has NO member in the lane's population
-- @fix define-class with a member from the lane's population, or ship the value as policy with its reason
CREATE VIEW x_measured_without_representation AS
  SELECT s.shipped_id, s.lane, s.content_class_id
    FROM shipped s
   WHERE s.provenance = 'measured'
     AND NOT EXISTS (SELECT 1 FROM content_class_member m
                       JOIN window w ON w.window_id = m.window_id
                       JOIN v_lane_population p ON p.title_id = w.title_id AND p.lane = s.lane
                      WHERE m.content_class_id = s.content_class_id);

-- @check a `measured` value whose evidence class is not sampled for that lane
-- @fix name an evidence class that serves the lane (define-class lists its lanes), or ship the value as policy with its reason
CREATE VIEW x_measured_on_a_class_not_for_the_lane AS
  SELECT s.shipped_id, s.lane, s.content_class_id
    FROM shipped s
   WHERE s.provenance = 'measured'
     AND NOT EXISTS (SELECT 1 FROM content_class_lane cl
                      WHERE cl.content_class_id = s.content_class_id AND cl.lane = s.lane);

-- @check a constant applied outside its scope
-- @fix scope-constant the constant to the lane before a shipped setting cites it
CREATE VIEW x_constant_outside_scope AS
  SELECT s.shipped_id, s.lane, ss.from_constant
    FROM shipped s JOIN shipped_setting ss ON ss.shipped_id = s.shipped_id
   WHERE ss.from_constant IS NOT NULL
     AND NOT EXISTS (SELECT 1 FROM constant_scope c WHERE c.name = ss.from_constant AND c.lane = s.lane);

-- @check a run that covered a window outside its declared class
-- @fix plan the run over the class's members only; a window outside the class belongs to another run
CREATE VIEW x_run_outside_class AS
  SELECT rw.run_id, rw.window_id
    FROM run_window rw JOIN run r ON r.run_id = rw.run_id
   WHERE r.content_class_id IS NOT NULL
     AND NOT EXISTS (SELECT 1 FROM content_class_member m
                      WHERE m.content_class_id = r.content_class_id AND m.window_id = rw.window_id);

-- @check a cell on a window its run never declared covering
-- @fix plan the cell's window into the run before its cells; a cell on an uncovered window has no cut to encode
CREATE VIEW x_cell_outside_run_coverage AS
  SELECT c.cell_key, c.run_id, c.window_id
    FROM cell c
   WHERE NOT EXISTS (SELECT 1 FROM run_window rw WHERE rw.run_id = c.run_id AND rw.window_id = c.window_id);

-- @check a cell_setting value outside the setting's enumeration
-- @fix use one of the setting's enumerated values (add-setting lists them); the encoder would refuse the rest
CREATE VIEW x_setting_value_outside_enum AS
  SELECT cs.cell_key, cs.setting_id, cs.value
    FROM cell_setting cs JOIN setting s ON s.setting_id = cs.setting_id AND s.value_type = 'enum'
   WHERE NOT EXISTS (SELECT 1 FROM setting_enum_value e WHERE e.setting_id = cs.setting_id AND e.value = cs.value);

-- @check a cell whose quality anchor lies outside the setting's declared range -- a target past the encoder's range is UNREACHABLE, never a cell
-- @fix keep the anchor within the setting's range_lo..range_hi; report a target past the range as UNREACHABLE instead of planning a cell
CREATE VIEW x_cell_anchor_outside_range AS
  SELECT cs.cell_key, cs.setting_id, cs.value, s.range_lo, s.range_hi
    FROM cell_setting cs JOIN setting s ON s.setting_id = cs.setting_id AND s.kind = 'quality_anchor'
   WHERE (s.range_lo IS NOT NULL AND CAST(cs.value AS REAL) < s.range_lo)
      OR (s.range_hi IS NOT NULL AND CAST(cs.value AS REAL) > s.range_hi);

-- @check a reference cut built with the chain of a lane its class does not serve
-- @fix materialise the cut with the chain of a lane the class serves, or define-class with the chain's lane
CREATE VIEW x_cut_chain_not_a_served_lane AS
  SELECT c.cut_id, c.chain_lane
    FROM cut c JOIN content_class cc ON cc.reference_set_id = c.reference_set_id
   WHERE c.kind = 'reference'
     AND NOT EXISTS (SELECT 1 FROM content_class_lane cl
                      WHERE cl.content_class_id = cc.content_class_id AND cl.lane = c.chain_lane);

-- @check a class member with no reference cut in the class's reference set
-- @fix materialise the class's reference set over every member before define-class names them
CREATE VIEW x_member_without_reference_cut AS
  SELECT m.content_class_id, m.window_id
    FROM content_class_member m JOIN content_class cc ON cc.content_class_id = m.content_class_id
   WHERE NOT EXISTS (SELECT 1 FROM cut c
                      WHERE c.reference_set_id = cc.reference_set_id AND c.window_id = m.window_id AND c.kind = 'reference');

-- @check a class that serves no lane
-- @fix define-class with at least one lane; a class exists to give a lane its evidence
CREATE VIEW x_class_serves_no_lane AS
  SELECT cc.content_class_id FROM content_class cc
   WHERE NOT EXISTS (SELECT 1 FROM content_class_lane cl WHERE cl.content_class_id = cc.content_class_id);

-- @check a shipped encode step on a host with no chain for that lane and unit -- the build order could not emit a command
-- @fix author-chain for the lane on that host and unit before ship; the build order emits its command from the chain
CREATE VIEW x_shipped_without_chain AS
  SELECT s.shipped_id, s.lane, s.host, s.encoder_unit_id FROM shipped s
   WHERE s.step IN ('quality-target-encode','bitrate-target-encode')
     AND NOT EXISTS (SELECT 1 FROM chain c WHERE c.lane = s.lane AND c.host = s.host AND c.encoder_unit_id = s.encoder_unit_id);

-- @check a screen verdict taken under a base the unit is not MEASURED to honour on that window
-- @fix screen the base setting on that window first, to HONOURED; a verdict under an unhonoured base measures nothing
CREATE VIEW x_verdict_on_unmeasured_base AS
  SELECT v.verdict_id, v.setting_id, v.base_setting_id FROM setting_verdict v
   WHERE v.base_setting_id IS NOT NULL
     AND NOT EXISTS (SELECT 1 FROM setting_verdict b
                      WHERE b.encoder_unit_id = v.encoder_unit_id AND b.setting_id = v.base_setting_id
                        AND b.window_id = v.window_id AND b.verdict = 'HONOURED');

-- @check an arm using a setting the unit is not MEASURED to honour on any member of the class
-- @fix screen the setting on a member of the class to HONOURED before author-search puts it in an arm
CREATE VIEW x_arm_setting_not_honoured AS
  SELECT a.arm_id, ast.setting_id
    FROM arm_setting ast JOIN arm a ON a.arm_id = ast.arm_id JOIN search s ON s.search_id = a.search_id
   WHERE NOT EXISTS (SELECT 1 FROM setting_verdict v
                       JOIN content_class_member m ON m.window_id = v.window_id AND m.content_class_id = s.content_class_id
                      WHERE v.encoder_unit_id = s.encoder_unit_id AND v.setting_id = ast.setting_id AND v.verdict = 'HONOURED');

-- @check a derived ladder whose run is not a LOCATE run of the same search
-- @fix derive-ladders from the search's own locate run; a ladder derived from any other run is discarded
CREATE VIEW x_ladder_from_a_non_locate_run AS
  SELECT l.run_id, l.arm_id FROM arm_ladder_rung l JOIN run r ON r.run_id = l.run_id JOIN arm a ON a.arm_id = l.arm_id
   WHERE r.stage <> 'locate' OR r.search_id IS NOT a.search_id
   GROUP BY l.run_id, l.arm_id;

-- @check an (arm, window) ladder with fewer than four rungs -- bd_rate's floor
-- @fix widen the locate sweep until every (arm, window) has four rungs; bd_rate has no meaning below that
CREATE VIEW x_ladder_below_floor AS
  SELECT run_id, arm_id, window_id, count(*) AS rungs FROM arm_ladder_rung
   GROUP BY run_id, arm_id, window_id HAVING count(*) < 4;

-- @check a viewing verdict on an encode that was discarded, either of a pair or an acceptance's one -- scoring deletes; the viewing re-encodes and keeps
-- @fix record-viewing on kept encodes only: re-encode the cell in a viewing run, which keeps its output
CREATE VIEW x_viewing_on_a_discarded_encode AS
  SELECT g.viewing_id FROM viewing_verdict g
   WHERE NOT EXISTS (SELECT 1 FROM encode e WHERE e.cell_key = g.cell_a AND e.kept = 1)
      OR (g.cell_b IS NOT NULL
          AND NOT EXISTS (SELECT 1 FROM encode e WHERE e.cell_key = g.cell_b AND e.kept = 1));

-- @check a search without exactly one base arm
-- @fix author-search with exactly one arm of role base
CREATE VIEW x_search_arm_roles AS
  SELECT s.search_id, coalesce(sum(a.role = 'base'), 0) AS base_arms
    FROM search s LEFT JOIN arm a ON a.search_id = s.search_id
   GROUP BY s.search_id
  HAVING coalesce(sum(a.role = 'base'), 0) <> 1;

-- @check a shipping arm that belongs to another search
-- @fix set-shipping-arm with an arm of the same search
CREATE VIEW x_shipping_arm_not_in_search AS
  SELECT s.search_id, s.shipping_arm_id FROM search s JOIN arm a ON a.arm_id = s.shipping_arm_id
   WHERE a.search_id <> s.search_id;

-- @check a locate cell whose anchor value is not on the search's coarse ladder
-- @fix plan locate cells at the search's coarse rungs only (author-search lists them)
CREATE VIEW x_locate_cell_off_the_coarse_ladder AS
  SELECT c.cell_key, cs.value
    FROM cell c JOIN run r ON r.run_id = c.run_id AND r.stage = 'locate'
    JOIN search s ON s.search_id = r.search_id
    JOIN cell_setting cs ON cs.cell_key = c.cell_key AND cs.setting_id = s.anchor_setting_id
   WHERE NOT EXISTS (SELECT 1 FROM search_coarse_rung k
                      WHERE k.search_id = s.search_id AND k.rung = CAST(cs.value AS INTEGER));

-- @check a search serving an incumbent-bound lane without exactly one incumbent arm
-- @fix author-search with exactly one incumbent arm when a served lane's decision rule is incumbent
CREATE VIEW x_incumbent_rule_without_incumbent_arm AS
  SELECT s.search_id, cl.lane FROM search s
    JOIN content_class_lane cl ON cl.content_class_id = s.content_class_id
    JOIN lane l ON l.lane = cl.lane AND l.decision_rule = 'incumbent'
   WHERE (SELECT count(*) FROM arm a WHERE a.search_id = s.search_id AND a.role = 'incumbent') <> 1;

-- @check a search serving a target-bound lane with no target
-- @fix author-search with a target for the served target-bound lane, taken from its acceptance viewing
CREATE VIEW x_target_rule_without_targets AS
  SELECT s.search_id, cl.lane FROM search s
    JOIN content_class_lane cl ON cl.content_class_id = s.content_class_id
    JOIN lane l ON l.lane = cl.lane AND l.decision_rule = 'target'
   WHERE NOT EXISTS (SELECT 1 FROM search_target t WHERE t.search_id = s.search_id);

-- @check a measured constant that was never calibrated -- its value would be a typed number
-- @fix calibrate the constant from a calibrate run before a lane in its scope ships; a typed number is not a measurement
CREATE VIEW x_measured_constant_never_calibrated AS
  SELECT c.name FROM constant c
   WHERE c.provenance = 'measured'
     AND NOT EXISTS (SELECT 1 FROM constant_value v WHERE v.name = c.name);

-- @check a target-bound lane's target that does not come from an acceptance viewing for that lane
-- @fix record-viewing an acceptance for the lane first, then author-search with the target naming that viewing
CREATE VIEW x_target_without_a_viewing AS
  SELECT st.search_id, st.target, cl.lane
    FROM search_target st
    JOIN search s ON s.search_id = st.search_id
    JOIN content_class_lane cl ON cl.content_class_id = s.content_class_id
    JOIN lane l ON l.lane = cl.lane AND l.decision_rule = 'target'
   WHERE NOT EXISTS (SELECT 1 FROM viewing_verdict v
                      WHERE v.viewing_id = st.viewing_id AND v.kind = 'acceptance' AND v.lane = cl.lane);

-- @check an incumbent arm that no acceptance viewing, for a lane the search serves, found acceptable
-- @fix record-viewing an acceptance of the incumbent's encode for a served lane, then author-search naming it as accepted_by_viewing
CREATE VIEW x_incumbent_arm_not_viewed AS
  SELECT a.arm_id, a.search_id
    FROM arm a JOIN search s ON s.search_id = a.search_id
   WHERE a.role = 'incumbent'
     AND NOT EXISTS (SELECT 1 FROM viewing_verdict v
                       JOIN content_class_lane cl ON cl.lane = v.lane AND cl.content_class_id = s.content_class_id
                      WHERE v.viewing_id = a.accepted_by_viewing AND v.kind = 'acceptance' AND v.verdict = 'acceptable');

-- @check a shipped row naming a unit that is not in that host
-- @fix add-unit the unit on that host, or ship the unit the host has
CREATE VIEW x_shipped_unit_not_on_host AS
  SELECT s.shipped_id, s.host, s.encoder_unit_id FROM shipped s
   WHERE s.encoder_unit_id IS NOT NULL
     AND NOT EXISTS (SELECT 1 FROM host_unit hu WHERE hu.host = s.host AND hu.encoder_unit_id = s.encoder_unit_id);

-- @check a shipped lane with content that a unit on a host supports, with a step that has neither a shipped row nor an exclusion with a reason -- routing is complete once a lane ships anywhere
-- @fix ship the step on that host, or exclude-route the lane from it with the reason
CREATE VIEW x_supported_lane_not_routed AS
  SELECT DISTINCT l.lane, hu.host, ls.step
    FROM lane l
    JOIN lane_step ls ON ls.lane = l.lane
    JOIN encoder_unit u ON u.codec = l.codec
    JOIN host_unit hu ON hu.encoder_unit_id = u.encoder_unit_id
   WHERE l.has_content = 1
     AND EXISTS (SELECT 1 FROM shipped s2 WHERE s2.lane = l.lane)
     AND NOT EXISTS (SELECT 1 FROM shipped s WHERE s.lane = l.lane AND s.host = hu.host AND s.step = ls.step)
     AND NOT EXISTS (SELECT 1 FROM routing_exclusion x WHERE x.lane = l.lane AND x.host = hu.host);

-- @check a lane routed to a host and excluded from it at once
-- @fix ship a (lane, host) or exclude-route it, never both; an excluded route is not shipped
CREATE VIEW x_routed_and_excluded AS
  SELECT x.lane, x.host FROM routing_exclusion x
   WHERE EXISTS (SELECT 1 FROM shipped s WHERE s.lane = x.lane AND s.host = x.host);

-- @check a run marked complete with a cell still planned -- a completed measurement that never came home
-- @fix every planned cell needs an encode or a failure record before the complete event; post them, or post failed
CREATE VIEW x_complete_run_with_planned_cells AS
  SELECT r.run_id, count(*) AS still_planned FROM run r JOIN v_cell_state cs ON cs.run_id = r.run_id
   WHERE r.state = 'complete' AND cs.state = 'planned'
   GROUP BY r.run_id;

-- @check a run whose stored state is not its latest event -- the column and its log disagree
-- @fix a run's state changes only through an event; post the event and the column follows
CREATE VIEW x_run_state_disagrees_with_events AS
  SELECT r.run_id, r.state, e.state AS latest_event FROM run r
    JOIN run_event e ON e.run_id = r.run_id
   WHERE e.at = (SELECT max(at) FROM run_event e2 WHERE e2.run_id = r.run_id)
     AND e.state <> r.state
  UNION ALL
  SELECT r.run_id, r.state, NULL FROM run r
   WHERE r.state <> 'planned' AND NOT EXISTS (SELECT 1 FROM run_event e WHERE e.run_id = r.run_id);

-- @check a time, split or concurrency run active on a machine with any other active run -- the box is not quiet
-- @fix wait for the machine's other run to finish, or abandon it; a timing run runs alone on its machine, whichever runtime holds the other
CREATE VIEW x_timing_run_not_alone AS
  SELECT t.run_id AS timing_run, o.run_id AS other_run, ht.machine
    FROM run t JOIN host ht ON ht.host = t.host
    JOIN run o ON o.run_id <> t.run_id AND o.state IN ('launched','running')
    JOIN host ho ON ho.host = o.host AND ho.machine = ht.machine
   WHERE t.stage IN ('time','split','concurrency') AND t.state IN ('launched','running');

-- @check a run on a host that is blocked -- refused at the moment of use, with the fix
-- @fix unblock-host once the fix it names is done, or plan the run on another host
CREATE VIEW x_run_on_a_blocked_host AS
  SELECT r.run_id, h.host, h.blocked FROM run r JOIN host h ON h.host = r.host
   WHERE h.blocked IS NOT NULL AND r.state <> 'abandoned';

-- @check a score run on a host with no scorer row -- the plan could not say what to score with
-- @fix add-scorer for the host, or score on a host that has one
CREATE VIEW x_score_run_host_without_scorer AS
  SELECT r.run_id, r.host FROM run r
   WHERE r.stage = 'score' AND NOT EXISTS (SELECT 1 FROM scorer s WHERE s.host = r.host);

-- @check a search scored on two hosts with no equivalence between them marked exact for ssimulacra2 and butteraugli -- two scorers are one instrument only once measured so
-- @fix score the search on one scorer, or run equivalence between the two and split it only when exact
CREATE VIEW x_search_mixed_scorers_without_equivalence AS
  WITH scored_on AS (SELECT DISTINCT r.search_id, r.host FROM score sc JOIN run r ON r.run_id = sc.run_id)
  SELECT a.search_id, a.host AS scorer_a, b.host AS scorer_b, need.m AS metric
    FROM scored_on a JOIN scored_on b ON b.search_id = a.search_id AND b.host > a.host,
         (SELECT 'ssimulacra2' AS m UNION SELECT 'butteraugli') need
   WHERE NOT EXISTS (SELECT 1 FROM scorer_equivalence e
                       JOIN run ra ON ra.run_id = e.run_a JOIN run rb ON rb.run_id = e.run_b
                      WHERE e.metric = need.m AND e.exact = 1
                        AND ((ra.host = a.host AND rb.host = b.host) OR (ra.host = b.host AND rb.host = a.host)));

-- @check a run whose unit is not in the host it ran on -- a score run is exempt: its unit is its parent's, and its host holds the scorer
-- @fix plan the run on a host that has the unit (add-unit puts a unit on a host)
CREATE VIEW x_run_unit_not_on_host AS
  SELECT r.run_id, r.host, r.encoder_unit_id FROM run r
   WHERE r.encoder_unit_id IS NOT NULL AND r.stage <> 'score'
     AND NOT EXISTS (SELECT 1 FROM host_unit hu WHERE hu.host = r.host AND hu.encoder_unit_id = r.encoder_unit_id);

-- @check a screen verdict with no encodes behind it -- a probe that did not run is not evidence
-- @fix the screen posts a verdict with the cells it summarises; re-run the probe
CREATE VIEW x_verdict_without_cells AS
  SELECT v.verdict_id FROM setting_verdict v
   WHERE NOT EXISTS (SELECT 1 FROM setting_verdict_cell vc WHERE vc.verdict_id = v.verdict_id);

-- @check an encode with fewer frames than its cut -- a leg is verified by FRAME COUNT, never exit status
-- @fix the encode did not run to the end; read its stderr, fix the cause and re-encode the cell
CREATE VIEW x_encode_short_of_frames AS
  SELECT e.cell_key, e.frames, k.frames AS cut_frames
    FROM encode e JOIN cell c ON c.cell_key = e.cell_key
    JOIN run r ON r.run_id = c.run_id                        -- through the run's class, not its search: screen and viewing runs have none
    JOIN content_class cc ON cc.content_class_id = r.content_class_id
    JOIN cut k ON k.reference_set_id = cc.reference_set_id AND k.window_id = c.window_id AND k.kind = c.cut_kind
   WHERE e.frames <> k.frames;

-- @check a cell whose identity settings carry no rate-control mode -- the mode is derived from what is set, never read off argv
-- @fix plan the cell with its rate-control setting among the identity settings; a mode read off argv is not a setting
CREATE VIEW x_cell_without_a_rate_mode AS
  SELECT c.cell_key FROM cell c
   WHERE NOT EXISTS (SELECT 1 FROM cell_setting cs JOIN setting_role sr ON sr.setting_id = cs.setting_id
                      WHERE cs.cell_key = c.cell_key AND cs.role = 'identity' AND sr.canonical_id = 'rate_control_mode');

-- @check a search scored at a height that is not a served lane's panel height -- the height is a decision, never a default
-- @fix author-search with score_height equal to a served lane's score_height
CREATE VIEW x_search_height_not_a_lane_height AS
  SELECT s.search_id, s.score_height FROM search s
   WHERE NOT EXISTS (SELECT 1 FROM content_class_lane cl JOIN lane l ON l.lane = cl.lane
                      WHERE cl.content_class_id = s.content_class_id AND l.score_height = s.score_height);

-- @check a score at a height other than its search's -- rows carrying more than one height are refused
-- @fix score at the search's height only; the height is decided once, in author-search
CREATE VIEW x_score_at_another_height AS
  SELECT sc.run_id, sc.cell_key, sc.height, s.score_height FROM score sc
    JOIN run r ON r.run_id = sc.run_id JOIN search s ON s.search_id = r.search_id
   WHERE sc.height <> s.score_height;

-- @check a score run whose parent is not an encoding run of the same search and class
-- @fix score runs are planned from an encode, screen, locate or viewing run of the same search and class; re-plan it from one
CREATE VIEW x_score_run_without_parent AS
  SELECT s.run_id, s.parent_run_id FROM run s
   WHERE s.stage = 'score'
     AND NOT EXISTS (SELECT 1 FROM run p WHERE p.run_id = s.parent_run_id AND p.stage IN ('encode','screen','locate','viewing')
                      AND p.search_id IS s.search_id AND p.content_class_id IS s.content_class_id);

-- @check a reference cut in use with no content check passed or classified -- a faithful copy of a broken cut passes every sha
-- @fix verify the reference set to a passed content check, or classify-cut with the reason, before define-class uses the cut
CREATE VIEW x_reference_cut_unchecked AS
  SELECT k.cut_id FROM cut k JOIN content_class cc ON cc.reference_set_id = k.reference_set_id
    JOIN content_class_member m ON m.content_class_id = cc.content_class_id AND m.window_id = k.window_id
   WHERE k.kind = 'reference'
     AND NOT EXISTS (SELECT 1 FROM cut_check x WHERE x.cut_id = k.cut_id AND x.result IN ('pass','classified'));

-- @check an encode-stage reference encode discarded before it was scored -- staging is removed only on a clean finish
-- @fix keep the encode until its score record lands; staging is removed only on a clean finish
CREATE VIEW x_discarded_without_score AS
  SELECT e.cell_key FROM encode e JOIN cell c ON c.cell_key = e.cell_key JOIN run r ON r.run_id = c.run_id
   WHERE r.stage = 'encode' AND c.cut_kind = 'reference' AND e.kept = 0
     AND NOT EXISTS (SELECT 1 FROM score s WHERE s.cell_key = e.cell_key);

-- @check locate arms whose bitrate spans do not intersect on a window -- widen the locate sweep
-- @fix widen the locate sweep on that window until every arm's bitrate span overlaps the others'
CREATE VIEW x_arms_with_disjoint_bitrate_spans AS
  WITH spans AS (
    SELECT r.search_id, c.window_id, a.arm_id, min(e.bitrate_kbps) AS lo, max(e.bitrate_kbps) AS hi
      FROM cell c JOIN run r ON r.run_id = c.run_id AND r.stage = 'locate'
      JOIN encode e ON e.cell_key = c.cell_key
      JOIN arm a ON a.search_id = r.search_id AND a.role <> 'incumbent'
     WHERE NOT EXISTS (SELECT 1 FROM arm_setting ast WHERE ast.arm_id = a.arm_id
                         AND NOT EXISTS (SELECT 1 FROM cell_setting cs WHERE cs.cell_key = c.cell_key
                                            AND cs.setting_id = ast.setting_id AND cs.value = ast.value))
       AND (SELECT count(*) FROM arm_setting ast WHERE ast.arm_id = a.arm_id)
           = (SELECT count(*) FROM cell_setting cs JOIN setting st ON st.setting_id = cs.setting_id
               WHERE cs.cell_key = c.cell_key AND cs.role = 'identity' AND st.kind <> 'quality_anchor')
     GROUP BY r.search_id, c.window_id, a.arm_id)
  SELECT search_id, window_id, max(lo) AS highest_floor, min(hi) AS lowest_ceiling FROM spans
   GROUP BY search_id, window_id HAVING count(*) > 1 AND max(lo) > min(hi);
