# gpu-encoder-sweep

A measurement harness that finds the settings a GPU video encoder should ship, per content class and
encoder unit, and what each setting costs. Read the docs in this order: `docs/ARCHITECTURE.md` (how
it is built), `docs/SPEC.md` (the process it implements), `docs/DATA-MODEL.md` (the model, rendered
from `sweep/schema.sql`), `docs/refusals.json` (the invariants, each with the incident behind it).

## Rules that are structural here

- **`make check` is the gate**: the named unittest modules, `sweep/model_check.py --mutate` (every
  check must fire on its negative case) and `--check` (the rendered regions of `docs/DATA-MODEL.md`
  are current). Test modules are named, never discovered. No `-` prefixes, no `|| true`.
- **Edit `sweep/schema.sql`, never the rendered blocks** in `docs/DATA-MODEL.md`; re-render with
  `python3 sweep/model_check.py --render`.
- **Every refusal is a string starting `REFUSING: <what> -- <fix>`**, and every refusal has a test
  that names it. A check that never fires is not a check.
- **This repo is public.** No domain names, no credentials, no private paths beyond illustrative
  hostnames. The real catalogue exists only in the hub's database.

## Git

Commits here are granted by the user (2026-09-06: "you can commit to the new repo but not push").
**Never push**: `origin` is set, CI runs on every push, and a push needs the user's words at the time.

## Where the rest lives

The campaign this harness serves, its data, its results and the working notes are in the private
sibling repo `../media-library-discovery`: task docs in its `tasks/` (this repo has none; write there),
the old harness to lift from in `all-gpu-encoder-sweep/sweep.py` (the scoring recipe S1, the cell key
K1, progress parsing, the decode probe, the content hash, the launch traps), the committed CSVs the
Phase 5 acceptance compares against under `all-gpu-encoder-sweep/data/`, and the record export this
harness writes under `record/`. Its `CLAUDE.md` carries the fleet's traps; read it before touching a
node.

## Working style

Terse. Distinguish measured from assumed, and say which. Retract in place when a number turns out
wrong. Timestamps come from `date "+%Y-%m-%d %H:%M %Z"`, never inferred.
