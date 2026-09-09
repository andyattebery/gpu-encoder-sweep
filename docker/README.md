# The images

Four images, built and pushed to GHCR by `.github/workflows/images.yaml` on every push to `main` and on every `v*`
tag, as `ghcr.io/andyattebery/gpu-encoder-sweep-<image>:sha-<short sha>` (and `:main` or `:<tag>`). Every image
installs the package from `uv.lock` (`uv sync --locked`), the one place third-party packages are pinned, and writes
`/etc/sweep-artifact`: the artifact it is (`<image>:<version>`) and the git sha CI built from. The agent reports
both, and the hub hands a run only to an agent whose artifact is the one the plan was built for.

| image | base, pinned | adds | runs on |
|---|---|---|---|
| `hub` | `python:3.14-slim` by digest | the `hub` extra (fastapi, uvicorn, redis) | nas-01 |
| `node-encode` | `ghcr.io/haveagitgat/tdarr_node` by the digest `:latest` resolved to on 2026-09-07 — production's image, so the VAAPI driver and Mesa are production's | the linux64 portable jellyfin-ffmpeg at `v8.1.2-3+nvenc-n13.0.19.1` under `/opt/jellyfin-ffmpeg/bin`; python via uv; the `node` extra | media-01 |
| `node-encode-mesarc` | `ghcr.io/andyattebery/tdarr-node-mesa-fresh` by the digest `:mesarc` resolved to on 2026-09-08 — the same tdarr layer `node-encode` pins, with Mesa `26.2.2…~n~mesarc0` from `ppa:ernstp/mesarc`; it is what htpc-01 encodes with | the same as `node-encode`, plus a build-time assertion that our apt layer left `mesa-libgallium` and `radeonsi_drv_video.so` alone | htpc-01 |
| `node-score` | `nvidia/cuda:13.3.1-runtime-ubuntu26.04` by digest; FFVship built in a `13.3.1-devel` stage at Vship `v5.1.0` from Codeberg | FFVship and `libvship.so`; the same jellyfin-ffmpeg (`libvmaf_cuda`); python via uv; the `node` extra | media-01, eta-wsl |

A bump of any pin is deliberate: the tdarr digest must equal what the tdarr role pins, or the rig drifts from
production, and the mesa-fresh digest must equal what htpc-01's role pins for the same reason — `mesarc` is a
channel tag, so it moves, and the Mesa under it is what that box's `encoder_unit.driver` has to name; the FFVship
tag is the one the committed values were scored with, and a newer one is a new `scorer_build` the acceptance
comparison cannot run across; the jellyfin-ffmpeg release is the one current while the B580 lane's values were
made, to be checked against the ledgers' ffmpeg sha before the acceptance run (M3).

## What a role gives a container

The agent takes three environment variables and asks the hub for the rest (`GET /agents/{host}/config`):

- `SWEEP_HUB` — the hub's URL; `SWEEP_TOKEN` — this host's token, from the vault; `SWEEP_HOST` — the host row.
- The work root, mounted read-write at the path `add-host` gave as `work_root` (the same path inside and out keeps
  every plan's argv true on both sides); the share's `temp/harness/` read-write at `share_root`; for a score
  container on a machine with an encode runtime, the encode runtime's work root at `local_view`.
- The GPU: the nvidia runtime with `compute,utility,video` for NVIDIA cards; `/dev/dri` for the B580 and the 9070 XT.
- The hub: `/data` (the SQLite store and the per-frame values), `/share` = the pool's `temp/harness`, Redis in the
  same stack (`SWEEP_REDIS=redis://redis:6379`), the operator's token as `SWEEP_TOKEN`, the agents' tokens as a JSON
  file `{host: token}` named by `SWEEP_AGENT_TOKENS`, traefik labels for TLS.

The native Windows agent on eta is the same package with no image:
`uvx --from "gpu-encoder-sweep[node] @ git+https://github.com/andyattebery/gpu-encoder-sweep@<sha or tag>" sweep-node serve`
with the three variables set for the account, and uv installed by the role.

## Licences

The images vendor jellyfin-ffmpeg (GPL) and FFVship (Vship's licence); each keeps its own. The package is MIT.
