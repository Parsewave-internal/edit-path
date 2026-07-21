<!--
SPDX-FileCopyrightText: 2026 Video Path Pilot contributors
SPDX-License-Identifier: GPL-3.0-only
-->

# Edit Path: Development Reference

## Purpose

Edit Path is an experimental Kdenlive fork for collecting training data that
describes how an editor transforms source media into an edited video. A
**video path** is the ordered record of editor intent, interaction, and the
resulting project changes.

The long-term dataset unit is:

1. input assets;
2. ordered video-path events;
3. checkpoints or state differences;
4. the final project and rendered output.

The dataset vocabulary must remain independent of Kdenlive so paths can be
learned, compared, and eventually replayed in other editing systems.

## Repository baseline and licensing

The fork began at Kdenlive revision
`7de2ed9902b4288797a7781498546389a482a39e`. Kdenlive is GPL-3.0-or-later;
modified distributed binaries must be accompanied by corresponding source and
the applicable license notices. See `MODIFICATIONS.md` for the fork-specific
change inventory. Dataset ownership and privacy are separate from source-code
licensing and require their own agreements and handling policy.

## Version 1: semantic model hooks

### Design

Version 1 added calls to `VideoPathRecorder` inside selected Kdenlive timeline
model functions. It attempted to emit:

- `clip.insert`;
- `clip.move`;
- `clip.trim`;
- `clip.split`;
- `clip.delete`;
- `history.undo` and `history.redo`.

Events were written as append-only JSONL and flushed after every event. Each
event received a session ID, event ID, sequence number, UTC timestamp, timeline
ID, and operation parameters. Kdenlive's numeric IDs were explicitly marked as
session-scoped rather than portable dataset identities.

### Test result

Manual sessions `pilot-session-002.jsonl` and `pilot-session-004.jsonl` were
valid JSONL but each contained only `session.start` and one `history.undo`.
Normal clip operations performed through the UI did not reach the particular
instrumented overloads and branches.

### Conclusion

Version 1 failed the workflow-reconstruction acceptance criterion. Kdenlive
has separate paths for grouped clips, selection operations, fake/preview
moves, final moves, and calls with undo logging disabled. Leaf-function hooks
were not a reliable observation boundary. Undo capture worked because it was
closer to a shared command boundary.

This result should be retained: semantic hooks are valuable outcome evidence,
but isolated hooks cannot provide comprehensive collection.

## Version 2: hybrid interaction recorder

### Design goals

Version 2 records three complementary layers:

1. **Intent:** menu, toolbar, and keyboard commands represented by stable Qt
   action IDs where available.
2. **Interaction:** timeline clicks and drags represented as paired pointer
   gestures.
3. **Outcome:** the existing semantic timeline and undo events.

The authoritative production design remains `intent/interaction → state
change`. Version 2 improves intent and interaction coverage; canonical project
state differences remain future work.

### Central Qt capture

At application startup, `VideoPathRecorder` installs a global Qt event filter.
It discovers `QAction` objects and connects to their `triggered` signal. Actions
created later are discovered when child/show events occur.

`ui.command` records:

- `command_id`: Qt object name, or `unmapped` when none exists;
- visible label with mnemonic ampersands removed;
- invocation source: `keyboard`, `menu`, or `programmatic_or_unknown`;
- configured shortcuts;
- checked state;
- focused widget context;
- a unique interaction ID.

`ui.shortcut` records the portable key sequence, ambiguity flag, focused
widget, and interaction ID. Typed text is deliberately not recorded.

`ui.gesture` pairs mouse press and release events whose object ancestry belongs
to the timeline. It records click versus drag, target description, button,
modifier mask, and global start/end coordinates.

`session.end` is written on a normal application quit. A crash may leave a
valid prefix without `session.end`, which is useful diagnostic information.

### Schema versions

- `0.1.0`: Version 1 semantic/history events.
- `0.2.0`: hybrid UI, semantic/history, and session-end events.

The validator accepts both so historical pilot sessions remain inspectable.

### Privacy and security

The collector can reveal editor behavior, project structure, local file paths,
command labels, focused panels, and screen coordinates. Treat JSONL paths as
sensitive data. Do not collect passwords or raw typed text. Production use
requires informed editor consent, access control, retention limits, asset
licensing records, and a documented deletion process.

## Build and run

Build using the local Craft environment:

```bash
source /home/tenali/CraftRoot/craft/craftenv.sh
cmake --build /home/tenali/parsewave/kdenlive-video-path-pilot/build
```

Run with a new absolute output path for every session:

```bash
cd /home/tenali/parsewave/kdenlive-video-path-pilot
video-path-pilot/run-video-path-pilot.sh \
  /home/tenali/parsewave/pilot-session-005.jsonl
```

Validate after closing Kdenlive normally:

```bash
python3 video-path-pilot/validate_video_path.py \
  /home/tenali/parsewave/pilot-session-005.jsonl
```

## Version 2 manual acceptance test

1. Start a fresh recording and create/open a project.
2. Invoke one menu command and one toolbar action.
3. Invoke a known keyboard shortcut such as undo.
4. Drag a clip on the timeline and trim one edge.
5. Split or delete a clip.
6. Quit normally and validate the JSONL.
7. Confirm the file contains `ui.command`, `ui.shortcut`, `ui.gesture`, and
   `session.end`; semantic events are additional evidence, not the v2 gate.

## Known Version 2 limitations

- A command trigger does not prove that a project mutation succeeded.
- Global screen coordinates depend on layout, scaling, and monitor geometry.
- `unmapped` actions need stable IDs assigned before production collection.
- Source attribution uses a short timing window and may classify unusual
  asynchronous triggers as unknown.
- Timeline gestures do not yet identify the clip, edge, track, or frame under
  the pointer.
- Asset import and dialog field values are not represented semantically.
- There are no canonical timeline snapshots, state diffs, or replay engine.
- Semantic Version 1 hooks still miss grouped and specialized edit paths.

## Future development sequence

1. Add canonical UUIDs for assets, tracks, clip instances, effects, and
   sequences.
2. Capture normalized timeline snapshots before and after committed commands.
3. Compute state differences and link them to UI events with interaction IDs.
4. Resolve timeline coordinates to frame, track, clip, edge, and tool context.
5. Assign stable IDs to every dataset-relevant command.
6. Cover imports, effects, transitions, keyframes, audio, titles, captions,
   project settings, saving, and rendering.
7. Build replay and compare canonical state hashes after every step.
8. Add automated coverage reports showing which commands have both intent and
   outcome records.
9. Define dataset packaging, consent, privacy, licensing, review, and quality
   gates before scaling to hired editors.

## Maintenance rule

Update this file whenever the recorder architecture, event schema, acceptance
results, known limitations, build/run procedure, or roadmap changes. Do not
erase failed experiments; document what was attempted and why it changed.
