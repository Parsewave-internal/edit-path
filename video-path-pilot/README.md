<!--
SPDX-FileCopyrightText: 2026 Video Path Pilot contributors
SPDX-License-Identifier: GPL-3.0-only
-->

# Kdenlive Video Path Pilot

This fork includes a freeform recording MVP around Kdenlive. Editors can import,
download, or create media at any point. The app records canonical outcomes,
discovers actual resources from the final project, resolves them by SHA-256,
normalizes the accepted path, and generates `sample.json` automatically.

For the two-sample client trial, begin with `EDITOR_WORKFLOW.md`. The clean
format and language are in `sample.schema.json` and `VOCABULARY.md`.

## MVP recorder

The editor-facing interface is a native Qt desktop app. On Linux, double-click:

```text
video-path-pilot/run-collector-app.sh
```

Choose **Run** if the file manager asks whether to display or execute the file.
The app starts as a hidden supervisor: it creates a session folder and launches
blank Kdenlive directly with an isolated configuration. During editing it
creates a session-owned `edit.kdenlive` and records numbered segments. After
Kdenlive closes, the supervisor shows a completion or recovery screen that
validates termination and packages the completed sample. There is no assigned
job, initialization screen, or terminal workflow.

### Windows portable MVP

The editor deliverable is built by the manually triggered **Windows portable
MVP** GitHub Actions workflow. It uses the maintained KDE Craft Kdenlive
blueprint to compile this checkout and its dependencies for 64-bit Windows,
then adds an embedded Python runtime for local validation and sample packaging.
The uploaded artifact is `EditPath-Windows-x64.zip`.
The local/hosted build runs `EditPath.exe --self-test` before creating the ZIP;
the resulting `SELF-TEST.json` must report `passed: true`. A separate
`-PreflightOnly` mode checks the Windows machine before the long Craft build.

After extracting the archive, start `bin\\EditPath.exe`. Do not start
`bin\\kdenlive.exe` directly because that bypasses session supervision and
recording. The initial MVP artifact is unsigned, so Windows may display a
SmartScreen warning. Code signing and an installer are later distribution
steps; the portable package is the first functional test target.

Canonical state replay must reproduce every recorded state hash. Production
finalization reconstructs from the exact committed Kdenlive/MLT project-state
sidecar, renders `verification/reconstructed.mp4`, and compares it with the editor's
independent render using profile, duration, video SSIM, and audio metrics. It
then reconstructs the raw commands, shortcuts, gestures, and exact state
changes in a full nonlinear-editor training view as `edit-path/replay.mp4`. The replay
shows the project bin, real project monitor, effects/properties, multitrack
timeline, cursor motion, selections, keyboard feedback, and applied operations. This retains
effects, transitions, speed changes, keyframes, titles, and other project state
instead of reducing the edit to the GUI branch's limited cut/trim/move model.
The limited semantic adapter remains available only as a diagnostic report.

The underlying command interface remains available to developers and tests:

```bash
python3 video-path-pilot/sample_collector.py --help
```

`init` creates a workspace with content-addressed assets, `launch` starts the
recorder, `note` captures an occasional creative decision, and `finalize`
binds those assets to exact Kdenlive bin references and validates `sample.json`.
`inspect`, `reconstruct`, `process`, and `index` expose the production
reconstruction pipeline without removing any older collector commands.

`job_pipeline.py` discovers project resources and packages completed sessions.
It can also create controlled jobs for automated testing, but the editor GUI
does not require them. A verbal task becomes an explicit pending prompt in the
generated sample; the internal team attaches the exact wording later. Undo/redo
is retained in the generated behavior sample and raw evidence, while the
production `edit_path` bundle separately resolves the clean successful branch.

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
uses those stable IDs and the manifest's exact `bin_reference` mapping. Embedded
Kdenlive generators and sequences are identified separately so they remain
resolved without being misrepresented as external files.

## Build and run by operating system

The root [`README.md`](../README.md) contains the complete platform matrix and
end-to-end explanation.

On Windows, use the portable `EditPath-Windows-x64.zip` artifact and launch
`bin\EditPath.exe`. To create the artifact locally, follow
[`WINDOWS_BUILD.md`](../WINDOWS_BUILD.md). The portable package embeds Python
and ships Kdenlive, Melt/MLT, FFmpeg/FFprobe, and the reconstruction code.

On Linux, install the upstream Kdenlive build dependencies and the full MLT
plugin set, then build and start the supervisor:

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e .
cmake -S . -B build -GNinja -DBUILD_TESTING=OFF
cmake --build build --parallel
python -m edit_path doctor
./video-path-pilot/run-collector-app.sh
```

On openSUSE/WSL, also install `qt6-multimedia-imports`,
`libmlt7-modules`, `libmlt7-module-qt6`, and `frei0r-plugins`. A missing
QtMultimedia QML import is detected before launch because Kdenlive cannot
safely create its timeline without it. Use `docker run --init` for a persistent
GUI container so exited Kdenlive child processes are reaped.

When using KDE Craft, point the launcher at it instead of hard-coding a
machine-specific path:

```bash
export KDENLIVE_PILOT_CRAFT_ROOT=/absolute/path/to/CraftRoot
./video-path-pilot/run-collector-app.sh
```

On macOS, build the same targets using the dependencies in
[`dev-docs/build.md`](../dev-docs/build.md), then run
`EDIT_PATH_REPO_ROOT="$PWD" ./build/bin/EditPath`. macOS is currently a
developer source-build path; there is no signed/notarized EditPath bundle.
For a checkout-local Homebrew setup, run
`./packaging/macos/bootstrap-dependencies.sh`; it creates only `.venv` and
leaves other Kdenlive installations untouched.

For Linux/macOS recorder diagnostics, bypass the supervisor only when a raw
JSONL is specifically required:

```bash
./video-path-pilot/run-video-path-pilot.sh /absolute/path/session.jsonl
```

The diagnostic launcher refuses to append to an existing log, supplies the
runtime paths, and verifies usable font configuration. On a minimal openSUSE
container, install the required fonts with:

```bash
zypper --non-interactive install --no-recommends dejavu-fonts liberation-fonts
```

If the recorder log environment variable is absent, recording is disabled. Events are
appended and flushed after every line so a crash should not lose the whole
session. Exact states are written beside the log in `<log-name>-states/`; set
`KDENLIVE_VIDEO_PATH_STATE_DIR` to override that location. Checkpoint proxies
are enabled by default and may be disabled for recorder diagnostics with
`KDENLIVE_VIDEO_PATH_CHECKPOINT_PROXIES=0` (a production 0.3 session with them
disabled will not pass the checkpoint gate). Use a new output file for each
editor session.

Sidecar compression and checkpoint rendering run on a dedicated one-thread
pool. Pending work is bounded to two entries by default and completed futures
are reaped throughout the recording, so a multi-hour session does not retain
every project state or future in memory. The supervisor writes `session.json`
atomically and refreshes its heartbeat every 60 seconds.

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

In the freeform GUI flow, render one independent `.mp4`, `.mov`, `.mkv`, or
`.webm`, close Kdenlive, then click **Create Dataset Sample**. EditPath presets
the active session as the output destination and also discovers a different
path saved by Kdenlive, so the editor never has to move the render manually.
H.264 is optional. The finished trajectory and reconstruction are in
`completed-sample/edit-path/events.jsonl`,
`completed-sample/verification/reconstructed.kdenlive`,
`completed-sample/verification/reconstructed.mp4`, and the editing-process
`completed-sample/edit-path/replay.mp4`; the editor's target is always under
`completed-sample/outputs/`, and detailed media scores are in
`completed-sample/verification/report.json`.

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
