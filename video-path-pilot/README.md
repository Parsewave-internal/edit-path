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

`init` creates a workspace, `launch` starts the recorder, `note` captures an
occasional creative decision, and `finalize` normalizes and validates
`sample.json`. Undo/redo remains in evidence but is removed from the clean
successful trajectory.

The pilot is based on upstream Kdenlive revision
`7de2ed9902b4288797a7781498546389a482a39e`.

## Recorded operations

Version 2 interaction events:

- `ui.command` for discovered Qt actions;
- `ui.shortcut` for modified/function keys without raw typed text;
- `ui.gesture` for timeline clicks and drags;
- `session.end` on normal application exit.
- `state.checkpoint` for the canonical timeline baseline;
- `state.diff` after committed edits, undo, and redo.

Version 1 semantic events retained as outcome signals:

- `clip.insert`
- `clip.move` for a committed single-clip move
- `clip.trim`
- `clip.split`
- `clip.delete` for a simple single-clip deletion
- `history.undo` and `history.redo` for functional undo commands

Kdenlive's numeric object IDs are included as session-scoped replay handles.
They are explicitly not stable dataset identities. A production collector must
assign persistent UUIDs to assets, clip instances, tracks, and effects.

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
sessions in the same file.

If the environment variable is absent, recording is disabled. Events are
appended and flushed after every line so a crash should not lose the whole
session. Use a new output file for each editor session.

Validate a recorded session with:

```bash
python3 video-path-pilot/validate_video_path.py /absolute/path/session.jsonl
```

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

## Known gaps

- Asset import is not yet recorded. In the controlled MVP, copied assets must
  be imported in filename order and native references are bound by first use.
- Native IDs do not survive project reload or independent replay.
- Titles, subtitles, markers, bin identity, multicamera editing, advanced time
  remapping, render settings, and specialized effects are not yet covered.
- Undo/redo capture currently covers `FunctionalUndoCommand`; specialized Qt
  undo commands may not emit history events.
- There is no replay engine. Canonical timeline hashes exist, but production
  still requires persistent entity identities.
- The log contains file-system and project-derived identifiers. Treat it as
  potentially sensitive editor data.

The immediate gate is two complete human-reviewed samples for client feedback.
Persistent IDs and deterministic project-file asset resolution come before
scaling collection.
