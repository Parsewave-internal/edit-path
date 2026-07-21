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

### Version 2 manual test result

The first GUI test, `pilot-session-005.jsonl`, produced 33 valid schema `0.2.0`
events: one `session.start`, 13 `ui.command`, 18 `ui.gesture`, and one clean
`session.end`. Commands included media import, Razor, Slip, Ripple, Insert and
Normal modes, overwrite-to-in-point, and playback controls. Two drags and 16
clicks were paired successfully.

This is a partial acceptance pass. No `ui.shortcut` was observed, all commands
were classified as `programmatic_or_unknown`, and the timeline ancestry test
also classified some toolbar buttons and combo boxes as timeline gestures. No
Version 1 semantic outcome event was emitted. The next iteration must improve
input-source correlation, narrow gesture targeting to the QML timeline canvas,
and explicitly test a known shortcut.

## Version 2.1: input correlation and gesture filtering

Version 2.1 addresses the three defects found in session `005`:

- modified-key and function-key presses are captured centrally and deduplicated
  against Qt shortcut events without recording typed text;
- menu and toolbar mouse presses establish the input source before the action
  fires, allowing commands to be classified as `menu`, `toolbar`, `keyboard`,
  or `programmatic_or_unknown`;
- the shortcut and resulting command share an interaction ID;
- gestures are accepted only from Qt Quick objects whose ancestry belongs to
  the timeline, excluding widget-based toolbars and combo boxes.

The Version 2.1 acceptance test must deliberately invoke one menu command, one
toolbar command, and `Ctrl+Z`, then click and drag on the timeline. The expected
record contains each source classification, a `ui.shortcut` paired to its
keyboard `ui.command`, only QML timeline gesture targets, and `session.end`.

The first Version 2.1 run, `pilot-session-006.jsonl`, was force-terminated after
Kdenlive became unresponsive. Its valid seven-event prefix correctly classified
one menu and two toolbar commands, but ended shortly after Add Clip or Folder
and before shortcut/gesture coverage. No core dump was available. The recorder
was scheduling full QAction discovery on every Qt show/child event, creating a
plausible event-queue storm when a file dialog was opened. Action discovery is
now child-event-only and debounced so at most one scan is pending. This is a
preventive recorder fix; the available evidence does not prove sole causation.

The follow-up run, `pilot-session-007.jsonl`, completed normally with 40 valid
events and a clean `session.end`; the hang did not recur. It recorded one menu
command, one toolbar command, five timeline-only Qt Quick gestures (two clicks
and three drags), undo and redo history outcomes, and modified-key shortcuts.
This passes the core Version 2.1 capture gate. It also exposed two remaining
quality defects: modifier keys were emitted as noise (`Ctrl+Control` and
`Ctrl+Shift+Shift`), and shortcut events were not linked to their undo/redo
history outcome by interaction ID. These should be corrected before state-diff
development.

## Version 2.2: causal shortcut cleanup

Version 2.2 makes only the capture-time corrections that cannot be recovered
reliably offline. Standalone Control, Shift, Alt, and Meta key presses are no
longer emitted as shortcuts. Deliberate repeated shortcuts remain in the event
stream; broad deduplication belongs in the dataset parser. When undo or redo is
executed within one second of a shortcut, the history outcome now carries the
same `interaction_id`, preserving the causal link. Other normalization and
source cleanup remain offline responsibilities so state-diff work is not
delayed.

The confirmation run, `pilot-session-008.jsonl`, passed Version 2.2 acceptance.
It contained 11 valid events and a clean `session.end`. No modifier-only noise
was present. Two `Ctrl+Z` shortcuts and one `Ctrl+Shift+Z` shortcut each shared
their interaction ID with the corresponding `history.undo` or `history.redo`
outcome. The causal shortcut-cleanup milestone is therefore complete.

## Version 3: canonical timeline state differences

Version 3 adds an outcome layer at Kdenlive's central undo-command boundary.
After the GUI initializes, the recorder writes a `state.checkpoint` containing
a canonical snapshot and SHA-256 state hash. After successful committed
commands, undo, and redo, it serializes the current state again and writes a
`state.diff` only when the canonical state changed.

The pilot snapshot contains the active timeline ID and duration; tracks in
timeline order with kind, tag, lock state and native ID; clips in deterministic
order with asset reference, track, timeline position, duration, source in/out,
speed and clip state/type; and basic composition placement. Each diff contains
entity-level `added`, `removed`, and `updated` records with before/after values,
plus before/after state hashes and duration changes. A recent UI interaction ID
is attached when causality can be established within two seconds.

This boundary is broader than Version 1's leaf hooks because successful edits
from timeline, bin, effects, keyframes, markers, subtitles, and other systems
generally converge at `Core::pushUndo`. Functional undo and redo are captured
after their state mutation completes.

Version 3 remains a pilot canonicalization. Native IDs are session-scoped;
effects, keyframes, subtitles, markers, groups, transitions/mix parameters and
asset content hashes are not yet fully represented. The acceptance test is to
insert, move or trim a clip, undo and redo, then prove contiguous hash chaining:
each diff's `before_hash` must equal the previous checkpoint/diff `after_hash`,
and inverse operations must restore an earlier hash.

The first Version 3 run, `pilot-session-009.jsonl`, was valid and closed
normally but was inconclusive for state-diff acceptance. It produced a
`late-baseline` checkpoint with four tracks and no timeline clips, followed by
no `state.diff` events. The observed undo label was `Delete effect
Adecorrelate`; effects are not included in the Version 3 pilot snapshot. Media
import changes the bin rather than the timeline. The recorder therefore
correctly found no represented timeline-state change. The next test must drag
an imported clip from Project Bin onto V1 before moving, trimming, undoing, and
redoing it.

The follow-up `pilot-session-010.jsonl` captured a semantic `clip.insert` and
timeline gestures, then Kdenlive exited unexpectedly during a trim attempt. It
had no `state.diff`. Investigation showed that most timeline operations use the
`PUSH_UNDO` macro, which pushes directly to `DocUndoStack` and bypasses
`Core::pushUndo`; the original Version 3 boundary was therefore incomplete.
Commit-time state capture has been moved to `DocUndoStack::push`, the actual
shared boundary, and the redundant Core capture removed. The unexpected exit
occurred before any state-diff capture ran, so available evidence does not tie
it to snapshot serialization. No core dump was available.

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
