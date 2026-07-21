<!--
SPDX-FileCopyrightText: 2026 Video Path Pilot contributors
SPDX-License-Identifier: GPL-3.0-only
-->

# Kdenlive Video Path Pilot

This fork records a hybrid stream of Qt commands, shortcuts, timeline gestures,
and a small software-independent subset of semantic editing operations as
newline-delimited JSON (JSONL). It is a feasibility pilot, not a complete
training-data collector. See `documentation.md` for the complete development
history, architecture, test results, limitations, and roadmap.

The pilot is based on upstream Kdenlive revision
`7de2ed9902b4288797a7781498546389a482a39e`.

## Recorded operations

Version 2 interaction events:

- `ui.command` for discovered Qt actions;
- `ui.shortcut` without raw typed text;
- `ui.gesture` for timeline clicks and drags;
- `session.end` on normal application exit.

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

- Asset import is not yet recorded; `clip.insert.asset_reference` is Kdenlive's
  bin reference rather than a content-addressed asset identity.
- Native IDs do not survive project reload or independent replay.
- Group moves, selection deletes, compositions, transitions, effects,
  keyframes, audio mixing, captions, color operations, and project checkpoints
  are not covered.
- Undo/redo capture currently covers `FunctionalUndoCommand`; specialized Qt
  undo commands may not emit history events.
- There is no replay engine or canonical timeline-state hash yet.
- The log contains file-system and project-derived identifiers. Treat it as
  potentially sensitive editor data.

The next gate is not broader instrumentation. It is persistent identities plus
a replay test proving that the six pilot operations reconstruct the same
timeline state.

