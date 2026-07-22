<!--
SPDX-FileCopyrightText: 2026 Video Path Pilot contributors
SPDX-License-Identifier: GPL-3.0-only
-->

# Kdenlive Video Path Pilot

This fork includes an MVP training-sample collector around the Kdenlive
recorder. It packages the prompt, hashed assets, editor plan and rationale,
accepted software-independent edit path, final render, native project, and raw
audit evidence into one sample directory.

For the two-sample client trial, begin with `EDITOR_WORKFLOW.md`. The clean
format and language are in `sample.schema.json` and `VOCABULARY.md`.

## MVP collector

```bash
python3 video-path-pilot/sample_collector.py --help
```

`init` creates a workspace with content-addressed assets, `launch` starts the
recorder, `note` captures an occasional creative decision, and `finalize`
binds those assets to exact Kdenlive bin references and validates `sample.json`.
`inspect`, `reconstruct`, `process`, and `index` expose the production
reconstruction pipeline without removing any older collector commands.

The pilot is based on upstream Kdenlive revision
`7de2ed9902b4288797a7781498546389a482a39e`.

## Recorded format

Schema 0.3 adds the prerequisites for deterministic reconstruction:

- content-addressed Zstandard Kdenlive/MLT state sidecars on checkpoints and
  committed state changes (Qt compression remains a build-time fallback);
- independent low-resolution checkpoint renders, produced asynchronously;
- stable entity IDs plus exact transaction and undo-entry IDs;
- global project hashes as well as per-timeline semantic hash chains;
- a one-time `project.context` with frame/profile and tool versions;
- explicit `session.abort` and `session.recovered` lifecycle evidence;
- `session.end.state_sidecars_complete`, written only after asynchronous state
  and reference artifacts have finished.

Schema 0.1 and 0.2 remain readable and validatable. A finalized legacy sample
can still reconstruct from `internal/final.kdenlive`; only 0.3 recordings are
required to contain the stronger transaction, state-sidecar, and checkpoint
evidence.

Version 1 semantic events retained as outcome signals:

- `clip.insert`
- `clip.move` for a committed single-clip move
- `clip.trim`
- `clip.split`
- `clip.delete` for a simple single-clip deletion
- `history.undo` and `history.redo` for functional undo commands

Kdenlive's numeric object IDs remain as session-scoped diagnostic handles for
backward compatibility. Schema 0.3 also records UUID entity IDs; normalization
uses those stable IDs and the manifest's exact `bin_reference` mapping.

## Build

The local Craft dependency environment can be used without its activation
script:

```bash
export PATH=/home/tenali/CraftRoot/dev-utils/bin:/home/tenali/CraftRoot/bin:/home/tenali/CraftRoot/libexec:$PATH
export LD_LIBRARY_PATH=/home/tenali/CraftRoot/lib:/home/tenali/CraftRoot/usr/lib/x86_64-linux-gnu${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}
export PKG_CONFIG_PATH=/home/tenali/CraftRoot/lib/pkgconfig:/home/tenali/CraftRoot/share/pkgconfig
export FONTCONFIG_FILE=/home/tenali/CraftRoot/etc/fonts/fonts.conf
export FONTCONFIG_PATH=/home/tenali/CraftRoot/etc/fonts

cmake -S . -B build -GNinja \
  -DBUILD_TESTING=OFF \
  -DCMAKE_DISABLE_FIND_PACKAGE_KF6DocTools=ON \
  -DCMAKE_PREFIX_PATH=/home/tenali/CraftRoot \
  -DCMAKE_REQUIRED_INCLUDES=/home/tenali/CraftRoot/include \
  -DCMAKE_C_STANDARD_INCLUDE_DIRECTORIES=/home/tenali/CraftRoot/include \
  -DCMAKE_CXX_STANDARD_INCLUDE_DIRECTORIES=/home/tenali/CraftRoot/include
cmake --build build --parallel 4
```

DocTools is disabled only because the manually activated Craft environment does
not expose its XML catalog. It does not affect the application or recorder.

## Run

Start the built application with a fresh absolute log path:

```bash
video-path-pilot/run-video-path-pilot.sh /absolute/path/session.jsonl
```

The launcher supplies the Craft runtime library paths and refuses to mix two
sessions in the same file. It also selects a readable system fontconfig file
when Craft does not provide one and refuses to launch if no usable UI font is
installed. On a minimal openSUSE container, install the required fonts with:

```bash
zypper --non-interactive install --no-recommends dejavu-fonts liberation-fonts
```

If the environment variable is absent, recording is disabled. Events are
appended and flushed after every line so a crash should not lose the whole
session. Exact states are written beside the log in `<log-name>-states/`; set
`KDENLIVE_VIDEO_PATH_STATE_DIR` to override that location. Checkpoint proxies
are enabled by default and may be disabled for recorder diagnostics with
`KDENLIVE_VIDEO_PATH_CHECKPOINT_PROXIES=0` (a production 0.3 session with them
disabled will not pass the checkpoint gate). Use a new output file for each
editor session.

Validate a recorded session with:

```bash
python3 video-path-pilot/validate_video_path.py /absolute/path/session.jsonl
```

## Reconstruct and publish

The full implementation and operator commands are documented in
`RECONSTRUCTION.md`. The short path after `finalize` is:

```bash
python3 -m edit_path inspect /path/to/sample
python3 -m edit_path process /path/to/sample /path/to/dataset
python3 -m edit_path index /path/to/dataset
```

Successful samples are atomically published under `accepted/<session_id>/`.
Failures are atomically published under `quarantine/<session_id>/` with a
machine-readable `rejection.json` naming the failed gate and sequence.

## Manual acceptance scenario

1. Create a fresh project and import two short media files.
2. Insert both files into the timeline.
3. Move one clip to another position.
4. Trim the start and end of a clip.
5. Split a clip.
6. Delete one resulting segment.
7. Undo and redo the deletion.
8. Exit Kdenlive and run the validator.

The JSONL should have a single `session.start`, contiguous sequence numbers,
UI command/shortcut/gesture events in editor order, and `session.end` after a
normal exit. Semantic actions may still be absent due to the Version 1 coverage
limitations documented in `documentation.md`.

## Deliberate boundaries

- Reconstruction uses exact state, never UI gesture replay. Semantic action
  coverage can expand without changing render determinism.
- The asset manifest records `license_status: pending`, but licensing is not an
  acceptance gate unless the operator explicitly passes `--require-license`.
- The project profile has no authoritative audio sample-rate field before a
  render preset is selected, so capture records it as null instead of guessing.
- Specialized editing features still depend on Kdenlive's exact MLT snapshot
  for rendering even when they do not yet have a fine-grained vocabulary label.
- The log contains file-system and project-derived identifiers. Treat raw
  sessions as potentially sensitive editor data.
