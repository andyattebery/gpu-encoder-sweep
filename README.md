# gpu-encoder-sweep

A measurement harness that finds the settings a GPU video encoder should ship — per content class,
per encoder unit (vendor, card, driver, frontend, codec), at the height the device shows — and what
each setting costs in bits and speed. It produces an iso-quality lookup table and the per-lane values
a transcode flow runs.

| doc | what it answers |
|---|---|
| `docs/SPEC.md` | the process: invariants, stages 0–11 with what each reads, writes, decides and refuses, and the recipes |
| `docs/DATA-MODEL.md` | the model: the two keys, the sample, the schema (rendered from `sweep/schema.sql`), the checks |
| `docs/ARCHITECTURE.md` | how it is built: the hub service, the agents, the queue, the exchange, the images, deployment |
| `docs/refusals.json` | the invariants, each with the incident that bought it and how the harness closes it |

    uv sync --locked --extra hub --extra cli --extra node    # the locked environment: uv.lock and .python-version are the one pin
    uv run make check                           # the tests, the model proof (every check fires on its negative case), the docs current

Status: M2 — the schema and its proof, the hub (every authoring and mechanical verb, ingest, the
Redis queue, the agent endpoints, export), the CLI, the node agent (proven end to end in-process with
fake tools, and on a real Redis under `make integration`) and the three images are here; the
two-cell smoke on real hardware waits on the infrastructure roles. The acceptance run (M3) follows.
The images vendor jellyfin-ffmpeg (GPL) and FFVship, which keep their own licences.
