# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

A pixi-managed Python 3.10 environment for developing against Unitree humanoid robots (G1 / R1 etc.) with `unitree_sdk2py`. There is no build step, linter, or test suite — the repo is the environment (`pixi.toml`/`pixi.lock`) plus small runnable scripts. README.md (Japanese) is the user-facing setup guide; `docs/` holds an operations guide PDF.

## Commands

All commands run through pixi (platforms: linux-64, osx-64, osx-arm64, win-64):

```bash
pixi install                                  # create env from pixi.lock
pixi run verify-sdk                           # confirm unitree_sdk2py + cyclonedds import
pixi run sample-tts -i <iface>                # TTS sample
pixi run sample-walk -i <iface>               # walking sample (moves the real robot!)
pixi run python scripts/<name>.py -i <iface>  # run any custom script
```

New frequently-run scripts can be registered under `[tasks]` in `pixi.toml`.

## Architecture / key constraints

- **`cyclonedds==0.10.2` is pinned deliberately.** The robot's onboard nodes run CycloneDDS 0.10.2; newer 11.x has mismatched XTypes type hashes and cannot communicate. Python is pinned to 3.10 because that is where the prebuilt wheel exists. Don't bump either.
- `unitree_sdk2py` is installed from git (PyPI deps in `pixi.toml`), not from a release.
- **Network interface is passed in code, not via XML.** `ChannelFactoryInitialize(0, iface)` loads a fixed internal CycloneDDS config, so `CYCLONEDDS_URI` / `cyclonedds.osx.xml` is not read. Scripts take `-i/--interface` (e.g. `en7`, `eth0`) and pass it through.
- Network setup: robot `192.168.123.161`, dev PC static `192.168.123.164/24`, wired Ethernet. Run all code on the dev PC; never develop on the robot's onboard PC (it runs real-time control).
- Scripts follow the pattern in `scripts/template.py` (argparse `-i`, `ChannelFactoryInitialize(0, args.interface)`); copy it for new programs. Clients (e.g. `LocoClient` from `unitree_sdk2py.g1.loco.g1_loco_client`) need `SetTimeout()`, `Init()`, then a short sleep for DDS discovery. `Move()` has a ~1 s default validity, so continuous motion requires re-sending it periodically (see `walk()` in `samples/sample_walk.py`) and finishing with `StopMove()`.
- **Demo scenario scripts live in `scripts/demo/`**: one script per phase (`phase0_approach` … `phase4_release`) plus the root `run_demo.py`. Phases share state only through `logs/state.json` and append every step to `logs/events.jsonl`; robot-moving phases are dry-run unless `--execute`. See `docs/demo_scripts.md`. Other files in `scripts/` are the recognition/control libraries and tools these reuse.
- TTS (`TtsMaker`) supports only speaker_id 0 (Chinese) and 1 (English); no Japanese.
