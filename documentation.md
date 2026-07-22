<!--
SPDX-FileCopyrightText: 2026 Video Path Pilot contributors
SPDX-License-Identifier: GPL-3.0-only
-->

# Edit Path: Development Reference

## Purpose

Edit Path is an experimental Kdenlive fork for collecting training data that
describes how an editor transforms source media into an edited video. In the
current MVP, a **video path** is the ordered record of editor interactions and
resulting project changes. Editor plans, explanations, decisions, and intent
are not collected.

The current dataset item is:

1. input assets;
2. ordered video-path events;
3. checkpoints or state differences;
4. the final project and rendered output.

The dataset vocabulary must remain independent of Kdenlive so paths can be
learned, compared, and eventually replayed in other editing systems.
`sample.json` is authoritative, and the role-oriented on-disk organization is
defined in `DATASET_ITEM.md`. Sections below named Version 1 or Version 2 are a
historical development record and may describe experiments that are not part
of the current editor workflow.

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

The isolated insertion run, `pilot-session-011.jsonl`, passed the corrected
Version 3 commit boundary. It produced a clean hash chain from a zero-clip
checkpoint to one `Insert Clip` state diff and closed normally. Because the
source contained audio and video, the diff correctly added two linked native
clip instances: a video item on track 3 and an audio item on track 2. Both were
240 frames with source range 0–239 and the timeline duration changed from 0 to
719 frames. This proves canonical insertion outcome capture; movement and
reversible hash restoration remain separate acceptance gates.

The movement run, `pilot-session-012.jsonl`, passed the remaining movement and
reversibility gates. After insertion added linked audio/video instances at
frame 431, a grouped move updated both to frame 892. Undo restored the exact
post-insertion hash and redo restored the exact post-move hash. All four diffs
formed a contiguous chain, the move/undo/redo diffs carried interaction IDs,
validation succeeded, and shutdown was clean. Canonical insertion and grouped
movement are therefore proven reversible for this pilot representation.

The trim run, `pilot-session-013.jsonl`, passed canonical resize and reversible
hash restoration. A grouped resize changed both linked AV clips from 240 to 108
frames and their source end from 239 to 107. Undo restored the exact pre-trim
hash and redo restored the trimmed hash. An unintended 90-frame composition
was also inserted earlier in the session and was represented correctly in the
chain. Kdenlive temporarily displayed a not-responding prompt during the
interactive trim, then recovered and closed normally. Undo/redo snapshot and
diff events followed their history events within approximately 0–1 ms, while
the pause occurred before the resize commit boundary; current evidence points
to the interactive Kdenlive trim path rather than JSON serialization.

## Version 3.1: expanded edit coverage

Version 3.1 extends the canonical snapshot instead of adding separate hooks for
each editing tool. Clip effect stacks now include ordered effects and all
serialized parameters, including animation/keyframe strings. Track and master
effect stacks are represented in the same form. Compositions now include their
software asset ID and complete parameter JSON in addition to placement and
duration. XML-derived effect data is converted to deterministic JSON with
sorted attribute names before hashing.

The existing central undo boundary already observes committed split, delete,
ripple delete, speed, fade, track, effect and transition commands. With the
expanded snapshot, these operations produce a `state.diff` whenever their
canonical outcome changes. Clip effects appear inside the changed clip, track
effects inside the changed track, master effects as the `master_effect` entity,
and transitions as compositions. Undo and redo must restore the exact earlier
hash as in the Version 3 tests.

The first Version 3.1 acceptance run should remain focused: insert one video
clip, split it, delete one resulting section, undo and redo the delete, then add
one readily available effect to the remaining clip and change one numeric
parameter. Undo and redo the parameter change and close normally. This proves
structural commands and parameterized effect state before testing transitions,
keyframes, speed, audio and track-level effects separately.

The first expanded-coverage run, `pilot-session-015.jsonl`, was valid and
closed normally. Splitting a linked AV clip produced two shortened original
instances and two new instances with contiguous source ranges. Deleting the
new linked pair and undoing that deletion restored the exact pre-delete hash.
Adding the Transform effect updated the selected video clip from zero to one
serialized effect; undo removed it and redo restored the exact post-addition
hash. The run did not contain a deletion redo or a committed numeric effect
parameter change, so those two gates remain untested rather than failed.

The parameter follow-up, `pilot-session-016.jsonl`, was valid and closed
normally. It captured adding the Adecorrelate effect and repeated exact hash
changes while disabling and enabling its effect stack. Inspection revealed
that the canonical XML conversion preserved property element names and
attributes but omitted their text-node values, where effect parameter values
are stored. Version 3.1 was corrected to serialize direct text and CDATA nodes
as deterministic `text` fields. Numeric parameter capture therefore requires a
new acceptance run with the corrected binary.

The corrected run, `pilot-session-017.jsonl`, was valid and closed normally.
It confirmed that effect property text is now present: Adenorm serialized its
level as `-351`, thread count as `0`, and position mode as `frame`. Transform
also recorded effect-zone parameters, first adding an in/out range of 0–90 and
then committing successive out values of 91, 92 and 93. The run additionally
captured effect addition/removal, stack enable/disable, track locking and clip
duplication. Its three shortcut events were effect or track controls rather
than undo/redo, so parameter serialization is accepted but reversible
parameter editing remains an optional follow-up gate.

The transition run, `pilot-session-018.jsonl`, was valid and eventually created
a same-track mix after selection and source-handle difficulties. The original
snapshot represented the associated clip extensions but emitted no composition
entity because Kdenlive stores same-track mixes in `TrackModel`, separately
from regular cross-track compositions. Version 3.1 now exposes that existing
read-only mix XML through `TimelineModel` and records a dedicated `mix` entity.
It includes the transition asset, first and second clip IDs, track IDs,
mix start/end/offset and every parameter value. Regular compositions remain
separate. A corrected run is required to accept mix creation and resizing.

The corrected transition run, `pilot-session-019.jsonl`, passed. Creating the
linked AV mix added a video `luma` entity and an audio `mix` entity. Both
contained their first/second clip IDs, track IDs, mix start/end, 30-frame
offset, MLT track mapping and complete parameter values. Removing the mixes
removed these entities independently. Undo of the final audio-mix removal
restored its exact prior hash, and redo restored the no-mix hash. Validation
and normal shutdown succeeded, so same-track mix creation, deletion and
reversibility are accepted.

The speed run, `pilot-session-020.jsonl`, passed. Changing speed to 150 percent
updated both linked video and audio instances from speed `1.0` and 200 frames
to speed `1.5` and 133 frames, with source end changing from 199 to 132. Undo
restored the exact post-insertion hash and redo restored the exact speed-change
hash. The chain validated and shutdown was clean, so linked clip speed changes
and reversibility are accepted.

The audio-fade run, `pilot-session-021.jsonl`, ended in an unexpected Kdenlive
exit. Its five complete JSONL records stop immediately after clip insertion;
there is no fade gesture, fade commit, undo/redo or `session.end`, and no system
core dump was available. The run is inconclusive and provides no evidence that
snapshot serialization caused the exit. It exposed a validation gap: a
structurally parseable crash fragment was previously reported as valid. The
validator now requires exactly one final `session.end` and explicitly reports
missing termination as an incomplete crashed/force-quit session. The fade test
is deferred rather than immediately repeated.

The voluntary retry, `pilot-session-022.jsonl`, reproduced the fade crash. It
ends after one QML fade-handle click and before any Adjust Fade command,
effect-state diff or recorder snapshot. Source tracing found that the build-tree
launcher did not set `QT_DATA_DIRS`; effect discovery therefore searched
`/kdenlive/effects` while built-in fade definitions reside in the checkout's
`data/effects`. The asset resolver now supports a source data root as well as
the installed `share/kdenlive` layout, and the launcher points it at the
checkout data directory. This is the leading cause, pending a post-fix startup
and fade check; the evidence still places both crashes before serialization.

The post-fix run, `pilot-session-023.jsonl`, confirmed the diagnosis and passed
audio-fade creation. Kdenlive remained stable and closed normally after adding
`fadein` to the audio clip. The canonical effect contained a 0–75 frame range,
gain endpoints 0 and 1, and complete in/out property values. Repeated effect
stack disable/enable operations alternated between the exact same two hashes.
This functionally confirms source-tree asset loading and accepts audio fade
creation/state capture; a dedicated fade-duration undo/redo cycle is no longer
required for the pilot because effect reversibility was established separately.

Before the formal track-state test, the canonical track object was extended
with audio `muted` and video `hidden` state. Track order, kind, label, lock and
effects were already present. Small read-only TimelineModel accessors expose
the existing TrackModel state without changing editing behavior.

The track-state run, `pilot-session-024.jsonl`, passed audio mute and video
visibility. A1 changed muted false→true→false and returned to the exact
post-insertion hash. V1 changed hidden false→true→false and returned to that
same hash. No lock click occurred in this run, but session 017 had already
captured repeated V1 lock/unlock changes with exact alternating hashes.
Together the runs accept canonical mute, visibility and lock state capture.

The track-structure run, `pilot-session-025.jsonl`, passed deletion and
restoration. Its late baseline already contained the newly inserted A3/V3
pair, so insertion was folded into the checkpoint rather than emitted as a
separate diff. Deleting the pair removed both tracks and deterministically
shifted the positions of A2, A1, V1 and V2. Undo restored the exact six-track
hash and redo restored the exact four-track hash. Track removal, reindexing and
undo reconstruction are accepted; explicit creation is represented by the
baseline and inverse added entities.

The ripple-delete run, `pilot-session-026.jsonl`, passed. Extracting the first
200-frame linked AV pair removed both instances and moved the second linked pair
from frame 359 to frame 159, exactly closing the deleted range. Undo restored
the exact pre-extract hash and redo restored the exact ripple-deleted hash.
Validation and normal shutdown succeeded, so multi-entity ripple deletion,
downstream movement and reversibility are accepted.

The final representative keyframe run, `pilot-session-028.jsonl`, passed
canonical keyframe capture. Adding Transform serialized its initial frame-zero
animation. Two explicit additions produced keyframes at frames 50 and 101 for
rotation, rotation anchor and rectangle/opacity. Editing the last keyframe
changed the rotation animation from `101=0` to `101=20`. Ctrl+Z and
Ctrl+Shift+Z were logged as UI shortcuts but did not reach Kdenlive's timeline
history, most likely because parameter-editor focus handled them locally, so
this run does not claim keyframe undo. Creation, timing and value capture are
accepted; reversibility is supported by the same serialized effect state and
has been proven for other effect and timeline operations.

## Step 1 pilot coverage conclusion

Representative canonical outcome capture is now accepted for linked clip
insertion, grouped movement and trim, split, ordinary and ripple deletion,
speed changes, effect addition and parameter values, effect keyframes, audio
fade, same-track video/audio mixes, regular composition placement, track
mute/visibility/lock, track deletion/reindexing, and undo/redo hash restoration
across multiple entity types. Raw menu commands, shortcuts and timeline mouse
gestures remain alongside these software-independent outcomes.

This concludes the planned Step 1 manual coverage pilot. It does not imply
complete Kdenlive feature coverage: titles, subtitles, markers, bin/project
asset identity, multicamera editing, advanced time remapping, rendering,
project settings and many specialized effects remain production roadmap work.
The next engineering phase should prioritize stable asset/instance UUIDs,
media hashes, crash-resilient session packaging, automated coverage reports
and privacy controls. Replay remains explicitly deferred by project decision.

## MVP sample collector (Version 4)

The project has moved from instrumentation research to a concrete product
gate: current editors will use an MVP collector to create two complete samples,
and the client will review those samples before production engineering or
hiring is scaled. A dataset item is consistently called a **sample** and its
primary client-facing representation is `sample.json`.

`video-path-pilot/sample_collector.py` implements the lifecycle. `init` creates
a new workspace, copies and hashes inputs, and captures the prompt, frame rate,
editor identity, and pre-edit plan. `launch` starts the instrumented Kdenlive
with crash-resilient JSONL evidence. `note` records selective creative reasons
and decisions. `finalize` requires a normally closed recording, native project,
rendered video, and editor review; it hashes the artifacts, normalizes the
successful branch, writes `sample.json`, and runs the quality gate.

```text
sample_001/
├── sample.json
├── assets/
├── output/final.*
├── internal/
│   ├── collector-metadata.json
│   ├── rationale.jsonl
│   └── final.kdenlive
└── evidence/raw-events.jsonl
```

The native project is internal evidence for diagnosis, recovery, and future
normalizer improvements. There is no `initial.kdenlive`: editors begin from a
blank project and the recorder establishes the canonical baseline. The final
video is mandatory because it is the target artifact and enables human review.

`normalize_sample.py` preserves undo and redo chronologically in the sample as
`history.undo` and `history.redo`, including their reverse/restored changes and
resulting hashes. A final-branch-only view can be derived later if required;
the MVP favors retaining more training information. Operations use integer frames,
software-independent entity names, sample-local canonical IDs, before/after
changes, resulting state hashes, and pointers to raw events. Kdenlive labels
are isolated under `extensions.kdenlive`. Ambiguous outcomes deliberately use
reviewable terms such as `clip.trim_or_split`, `effect.change`, or
`timeline.change` until two real samples show which distinctions are reliable.

The controlled MVP's former first-use asset-binding compromise has been
removed. Finalization reads native bin references from the saved project,
resolves resources by SHA-256, and packages them content-addressably. Recorder
entity IDs persist across recovery segments, and any used native reference that
cannot be bound causes finalization to fail closed.

The quality gate requires a rational frame rate, hashed inputs, at least one
accepted operation, no unresolved assets, complete raw termination, exact
project sidecars, a hashed project and independent render, and successful media
comparison. Freeform collection marks the verbal prompt pending until internal
staff attach its exact wording. Structural validity is not creative approval.

Operational instructions are in `video-path-pilot/EDITOR_WORKFLOW.md`, the
language in `video-path-pilot/VOCABULARY.md`, and the machine contract in
`video-path-pilot/sample.schema.json`. Automated tests cover branch compaction,
normalization, asset binding, hashes, and package validation.

### No-terminal GUI enhancement

Editor feedback changed the delivery requirement: the MVP must not expose a
terminal workflow. A native Qt 6 Widgets application wraps the tested collector
engine without duplicating its business logic. The current window supervises a
freeform isolated Kdenlive session, recovery, validation, exact reconstruction,
packaging, folder access, and visible task status; it does not ask the editor
for prompt, rationale, plan, or subjective review.

The implementation lives under `video-path-pilot/gui/`. The clickable
`run-collector-app.sh` launcher builds the GUI on first use against the same
Craft Qt environment used by Kdenlive and then launches it without requiring
typed commands. Python remains an internal runtime implementation detail;
editors do not type or see collector commands.

This GUI began on `feature/gui-collector-mvp` and is now integrated with the
schema-0.3 recorder and exact reconstruction architecture. The current launcher
is shareable among Linux machines with the project and dependencies. The
Windows Craft workflow produces the dependency-complete portable engineering
build; code signing and an installer remain separate distribution gates.

### MVP scope correction: recording only

The operational plan was narrowed after internal direction. Editors will be
given the application, task instructions, and assets; they will perform the
edit and return the recording, native project, render, and source assets. The
Parsewave team—not the editor—will construct the two canonical samples and ask
the client for feedback. Consequently, the editor-facing MVP must not collect
editor intent.

The GUI initialization, prompt, plan, asset-copying, rationale, decision-note,
subjective-review, normalization, and finalization screens were removed. The
app is now a one-screen **Edit Path Recorder** with Start Session and Open
Session Folder actions. Starting creates a unique directory under the user's
Videos folder, supplies a unique Kdenlive configuration so an old project is
not reopened, records JSONL and console evidence, and validates the raw session
after Kdenlive closes.

The sample schema and internal prototype were also revised so editor plan,
rationale notes, and subjective editor review cannot leak into generated
samples. The externally assigned task prompt remains valid sample input; it is
not collected from the editor application. Objective completion confirmation
and later internal human review remain quality-control concerns, not editor
intent.

### Assigned jobs, automatic packaging, and reconstruction foundation

The recorder now consumes a controlled `job.json` containing job ID, external
task prompt, project profile, and hashed asset manifest. Opening the job shows
the task and automatically imports its assets when an isolated Kdenlive session
starts. Editors do not identify or order assets manually.

`job_pipeline.py` creates and validates assigned jobs and packages completed
sessions. It parses both MLT `chain` and `producer` resources from the saved
`.kdenlive` XML, resolves each native bin ID to a job asset by SHA-256, and
rejects any unresolved native asset used by recorded operations. This replaces
the invalid first-use-order assumption exposed by the first GUI test. A pipeline
acceptance check reproduced that case: native ID 4 correctly resolved to
`asset_002`, not the first audio asset.

Clean operations are generated automatically after the editor closes Kdenlive
and clicks **Create Dataset Sample**. Numbered raw segments, project, render, assets, hashes,
and `sample.json` are packaged under `completed-sample/`. The validator checks
the resulting artifact paths and hashes.

The first reconstruction stage is implemented as independent canonical replay.
Starting at each recorded checkpoint, the pipeline applies the accepted state
diffs and recomputes deterministic SHA-256 timeline hashes after every step.
It also checks state continuity across crash-recovery segments. The replay was
verified against sessions 015, 019, 020, 023, 024, 025, 026, and 028 with exact
hash matches across clips, effects, mixes, speed, fades, track state/structure,
ripple delete, and keyframes.

Production finalization now uses the schema-0.3 exact committed project-state
sidecar as its reconstruction source. It remaps content-addressed assets,
renders `verification/reconstructed.mp4`, and compares that render with the
editor's independent output using profile, duration, video SSIM, and audio
metrics. It separately joins every raw command, shortcut, and pointer gesture
to its semantic before/after state and renders a complete nonlinear-editor
training replay as `edit-path/replay.mp4`. The view includes the project bin, exact-state
monitor, effects/properties, multitrack timeline, selection and cursor motion,
keyboard overlays, and applied-operation feedback. This preserves effects,
transitions, speed, keyframes, titles, and other serializable Kdenlive/MLT
state. The earlier cut/trim/move semantic adapter remains a useful diagnostic,
but it is no longer the acceptance authority.

Crash handling now preserves numbered JSONL and console segments. An invalid or
missing final `session.end` enables **Resume Editing**, which reuses the same
isolated Kdenlive configuration so its recovery mechanism can restore work.
Only the final segment must close normally; prior crash segments must remain
structurally valid and continuity is checked during canonical replay.
`session.json` persists the job, isolated configuration, segment number, process
ID, and lifecycle status so reopening the recorder can offer recovery or resume
finalization instead of losing supervisor state.

An end-to-end synthetic acceptance job exercised the complete supported path:
job creation and hashing, raw checkpoint/diff recording, project resource
resolution, clean operation generation, canonical replay, new MLT project
generation, rendering, decoded comparison, sample validation, and readiness
calculation. Native asset ID 4 resolved to `asset_001`, canonical replay passed,
media reconstruction passed with SSIM 0.974985 and audio PSNR 172.592 dB, every
packaged hash validated, and `quality.ready_for_client_review` was true.

### Freeform editor workflow correction

Testing showed that an assigned-job initialization screen was the wrong product
assumption. Editors may obtain or create assets throughout an edit rather than
receiving a complete manifest at startup. The editor GUI was reduced again to
Start New Edit, Resume Editing, Create Dataset Sample, and folder actions. It
launches blank isolated Kdenlive and does not preload media.

On **Create Dataset Sample**, `finalize-freeform` parses every file-backed `chain` and `producer`
from the saved project, deduplicates resources by SHA-256, assigns canonical
asset IDs, copies the discovered media, and resolves native IDs before sample
normalization. A freeform end-to-end test discovered the project asset,
generated the package, passed canonical and media reconstruction, and validated
all hashes.

Software cannot recover a verbal instruction. Therefore `sample.json` is still
generated at the editor end but contains `task.prompt: null`,
`prompt_status: pending_internal_entry`, and `ready_for_client_review: false`.
The internal `attach-prompt` operation inserts the exact known instruction and
recomputes readiness. The acceptance test changed readiness to true after that
attachment without collecting any editor intent.

The July 22 WSL code-11 failures were real crashes. The console logs reported a
missing `QtMultimedia` QML module followed by `Timeline root not created`, and
the kernel journal recorded Kdenlive segfaulting in QtCore. There was no OOM
evidence. Installing `qt6-multimedia-imports` removed those initialization
errors and the next segment closed normally. The supervisor and diagnostic
launcher now preflight this QML module and stop with an install instruction
before starting Kdenlive, rather than allowing a null timeline to reach the
segfault.

The first freeform interruption audit found a different failure mode in
`session_20260722_113348_7e8d3a1e`. Kdenlive stopped without `session.end`, the
manifest remained `recording`, no core dump was registered, and the console
ended while painting the imported clip. The JSONL lines that had already been
flushed survived, but the editor had never saved the initially untitled
project, so there was no project state for **Resume Editing** to reopen.

Crash recovery was consequently hardened around a session-owned project. A
new session now creates and opens `edit.kdenlive` automatically. On recovery,
the launcher passes that same file back to Kdenlive and starts the next
numbered JSONL/log segment. This stable project path also enables Kdenlive's
existing autosave/backup recovery to offer recent unsaved changes after a
force-kill. The editor should save normally and must not create a second
project file in the session folder. A GUI force-kill acceptance test is still
required because the autosave prompt and restored timeline cannot be verified
headlessly.

The first GUI run of that hardening exposed an initialization-order regression:
calling Kdenlive's save path immediately after `initGUI()` but before Qt's event
loop caused the application to exit during startup. The session correctly
became `recovery_available`, but contained only `session.start` and no project.
Project creation is now deferred until the GUI event loop is active; recovery
still passes an existing `edit.kdenlive` on Kdenlive's command line.

The editor-facing lifecycle was then simplified after the recorder wrapper
itself rendered black and became unresponsive over remote X11 before any new
session was created. The recorder is now a hidden supervisor during editing.
Launching the product opens Kdenlive directly and creates a session
automatically; only after Kdenlive exits does the supervisor show completion,
packaging, or recovery controls. A prior interrupted session offers **Resume
Editing** only when its session-owned `edit.kdenlive` exists. This removes
the redundant initialization screen from the normal editor workflow.

### Windows portable build

Windows is the editor deployment target. The supervisor now has a native
Windows launch path: it starts the adjacent `kdenlive.exe` directly with the
isolated recorder configuration and JSONL environment instead of invoking a
Bash script. Validation and finalization use `bin/python/python.exe`, an
embedded standard-library Python runtime included in the portable package.
Linux retains its development shell launcher; its recovery argument handling
was corrected to accept and reopen an existing project.

The manually triggered or `main`-push `.github/workflows/windows-portable.yml` workflow
bootstraps KDE Craft on a Windows 2022 runner, compiles this checkout through
the maintained Qt 6 Kdenlive blueprint, creates the dependency-complete Craft
archive, injects embedded Python, verifies `EditPath.exe` and `kdenlive.exe`,
and uploads `EditPath-Windows-x64.zip`. The first artifact is intentionally a
portable, unsigned engineering build. Installer creation, code signing, and
update delivery follow only after functional testing on the editor's machine.

To reduce first-artifact turnaround, the local build script now provides a
fast `-PreflightOnly` prerequisite check, appends a durable
`windows-build.log`, and prevents sleep while compiling. Before emitting the
ZIP it runs `EditPath.exe --self-test`, `kdenlive.exe --version`, invokes the
embedded Python validator, verifies FFmpeg-generated synthetic media, and
requires a passing `SELF-TEST.json`. This catches missing executables, packaged
scripts, Python runtime failures, and basic dependency-layout mistakes before
the editor receives the artifact.

### Linux portable build

`packaging/linux/build-appimage.sh` creates the portable x86_64 Linux MVP. The
AppImage contains EditPath, the modified Kdenlive, its installed application
data and QML modules, MLT plugins and profiles, FFmpeg/FFprobe, and a minimal
Python 3.11 runtime with Zstandard. Its `AppRun` establishes the internal MLT,
Qt, Python, and pipeline paths before opening EditPath. The package also carries
the XCB cursor library needed by Qt 6 and an offscreen Qt backend for headless
preflight testing. The optional CUPS print plugin is deliberately omitted
because editing and reconstruction do not use it and its Craft build requires
an unavailable `libcrypt.so.2` ABI.

The first locally built artifact passed `EditPath --self-test` from inside the
AppImage, including the bundled editor, media tools, QML module, validator,
sample pipeline, and reconstruction runtime. The build writes the AppImage and
its SHA-256 checksum to `linux-output/`; the folder is ignored by Git.

### Privacy and security

The collector can reveal editor behavior, project structure, local file paths,
command labels, focused panels, and screen coordinates. Treat JSONL paths as
sensitive data. Do not collect passwords or raw typed text. Production use
requires informed editor consent, access control, retention limits, asset
licensing records, and a documented deletion process.

## Current cross-platform build and run

The authoritative quick start and support matrix are in `README.md`. Windows is
the primary portable editor deployment: build with
`packaging/windows/build-editpath.ps1`, extract
`EditPath-Windows-x64.zip`, verify `SELF-TEST.json`, and start
`bin\EditPath.exe` rather than `kdenlive.exe`.

Linux supports both the validated source-build path and the engineering
AppImage described in `packaging/linux/README.md`. macOS uses the source path
but does not yet have a signed/notarized bundle. After installing the upstream
Kdenlive dependencies, the complete MLT plugins, FFmpeg/FFprobe, Python 3.10+
and Zstandard:

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e .
cmake -S . -B build -GNinja -DBUILD_TESTING=OFF
cmake --build build --parallel
python -m edit_path doctor
./video-path-pilot/run-collector-app.sh
```

Set `KDENLIVE_PILOT_CRAFT_ROOT=/absolute/path/to/CraftRoot` when using Craft.
On macOS, the full-build supervisor can also be started with
`EDIT_PATH_REPO_ROOT="$PWD" ./build/bin/EditPath`. The raw JSONL launcher is a
diagnostic path, not the normal editor experience:

```bash
./video-path-pilot/run-video-path-pilot.sh /absolute/path/session.jsonl
python3 video-path-pilot/validate_video_path.py /absolute/path/session.jsonl
```

Reconstruction can run natively on any OS with the required tools, or in the
pinned container on Linux, Windows Docker Desktop, and macOS Docker Desktop.
`./scripts/run-verification.sh` automatically uses the container for its real
media test when the host lacks Melt or FFmpeg.

## Current system operation

1. The EditPath supervisor creates a unique session and isolated Kdenlive
   configuration, then opens the session-owned `edit.kdenlive` project.
2. Schema-0.3 recorder hooks capture transaction-safe semantic diffs, undo/redo
   identity, exact compressed project states, project context, and independent
   checkpoint renders. The supervisor remains hidden while the editor works.
3. A crash preserves completed JSONL lines and state files. Recovery reopens
   the same project/configuration, keeps the session and entity IDs, and writes
   another numbered segment. Segment assembly rejects discontinuous state.
4. On Finish, the pipeline discovers saved-project resources, verifies them by
   SHA-256, packages content-addressed copies, and normalizes behavior evidence.
5. The accepted undo/redo branch selects an exact Kdenlive/MLT state. Melt
   renders it and FFmpeg/FFprobe compare checkpoints and the final render with
   independent editor output. This retains serializable effects, transitions,
   speed changes, keyframes, and titles when their MLT services are installed.
6. Passing output is atomically published. Any missing service, invalid chain,
   asset mismatch, or media mismatch produces a specific failure and preserves
   evidence for quarantine/review. The verbal prompt remains pending until
   internal staff attach the exact known instruction.

### Delayed semantic-action transaction fix

A Linux GUI recovery test exposed a schema-0.3 validation failure after a
normal clip insertion and clean shutdown. The state diff had the correct
transaction and undo-entry IDs, but the corresponding buffered `clip.insert`
action was written at shutdown without a `transaction_id`; the supervisor then
misclassified the otherwise clean recording as `recovery_available`.

`VideoPathRecorder::recordAction` still buffers actions emitted inside an active
transaction, but writes a just-completed action immediately with the preserved
transaction and undo-entry IDs. This prevents a later transaction flush from
overwriting the attribution. Reconstruction also safely repairs recordings
made before this fix when an otherwise-unassigned preceding diff has matching
semantics. Source-level and trajectory regression tests protect both paths.

### Supervisor GUI hardening after Linux acceptance

The follow-up GUI run validated the delayed-action transaction fix and produced
a clean `ready_to_finish` schema-0.3 session. It also exposed supervisor UX
problems specific to failure handling and forwarded X11. The supervisor now
distinguishes a nonzero/crash Kdenlive exit from a normal exit whose trajectory
fails validation, recording the latter as `validation_failed` instead of
incorrectly offering crash recovery. A ready-to-finish session labels the
new-session action as a discard and requires explicit confirmation, preventing
accidental empty sessions without trapping an operator who intentionally wants
to abandon unfinished work.

Folder buttons now copy their resolved path to the clipboard and report the
action in Activity; on forwarded-X11 Linux they try an installed file manager
before desktop URL dispatch, and show a visible fallback when neither is
available. The Linux launcher and child-editor environment also remove forced
`QSG_RHI_BACKEND` and `LIBGL_ALWAYS_SOFTWARE` overrides, which caused severe UI
stalls during the first optimized-build acceptance run.

### Explicit interrupted-session recovery and project checkpoints

A forced-quit acceptance run showed that the Linux desktop can terminate both
Kdenlive and its hidden parent supervisor. The session manifest, event segments,
state sidecars, and previously saved project survived, but restarting EditPath
silently opened another segment. That looked like a new session when the most
recent timeline work had not yet reached the project file.

EditPath now stops at a visible interrupted-session screen. It identifies the
existing session folder and requires the editor to choose **Recover and
Continue** before creating the next numbered recording segment. Starting a new
session remains possible, but is labelled as a discard and requires
confirmation. No recovery segment is launched silently.

Recorder-mode Kdenlive also writes the assigned `edit.kdenlive` itself whenever
the project is modified, no modal dialog is active, and the 30-second checkpoint
timer expires. This supplements Kdenlive's private stale-file recovery with an
unambiguous session-owned project that the supervisor can reopen. The interval
can be overridden for automated testing with
`KDENLIVE_VIDEO_PATH_AUTOSAVE_MS`, with a five-second safety minimum. A sudden
kill can still lose edits made since the latest checkpoint, but it no longer
depends solely on a manual Ctrl+S or an opaque stale file.

### GUI-ready feedback, watchdog restart, and reconstruction replay

Further remote-X11 testing showed that Kdenlive could take long enough to build
its interface that the hidden supervisor made startup look like a failure.
EditPath now stays visible with an indeterminate progress bar and a clear
startup message. Each recording segment receives a unique ready-signal path.
Kdenlive writes that signal atomically only after the active document has
finished its asynchronous load and the recorder has persisted a full timeline
checkpoint; only then does the supervisor hide. This is a real editor-and-data
readiness handshake rather than a fixed delay. In particular, it prevents the
first mutation after crash recovery from being consumed as a late baseline.
Kdenlive regenerates native object IDs and may reserialize equivalent project
XML during that reload. Segment assembly therefore permits an identity and
exact-byte-chain rebase only at an explicit recovery boundary, and only after
the normalized timeline snapshots prove that all editing semantics match.
Within each segment, state hashes and exact project-state chains remain strict.

Crash status is now persisted as `recovery_available` immediately when the
Kdenlive process exits abnormally, before asynchronous trajectory validation.
This closes the window in which a second forced termination could leave the
manifest incorrectly marked `recording`. On Linux, the normal collector
launcher also runs the GUI in a separate process session and restarts it after
an unexpected exit, with a three-restart limit. The persisted recovery screen
therefore reappears even when the desktop force-quits both visible GUI
processes. Normal application closure does not restart it.

The `features/video-reconstruction` work was integrated after the crash and
startup hardening baseline. In addition to exact final-state MLT reconstruction,
completed samples now render an editing-process replay from the accepted event
trajectory for training and review. Replay thumbnails are selected only from
assets that `ffprobe` confirms contain a video stream; audio-only inputs remain
in the manifest and timeline but are never connected to an FFmpeg video-filter
input. Offline reconstruction explicitly permits
MLT rendering without a display server. The final-state media gate remains
authoritative; process replays are derived artifacts and do not replace the
normalized trajectory or exact state chain.

Completed samples now use the role-oriented layout documented in
`DATASET_ITEM.md`. The editor target, edit-path replay, derived reconstruction,
and native evidence no longer share ambiguous top-level names. `sample.json`
remains the authoritative software-independent dataset record; verification
and provenance artifacts are explicitly separated from training inputs and
target output.

### Editor-facing recorder UI

The recorder window is designed around the editor's next action rather than
pipeline internals. Its primary actions are **Resume Editing**, **Create Dataset
Sample**, and **Open Dataset Sample**. Status messages state what happened,
whether the editor's work is safe, and what the editor should do next. Terms
such as hash chain, checkpoint, segment, traceback, and exact-state gate are
kept out of the main status area.

Detailed process output remains available through **Show technical details**
and in `supervisor-activity.log`, so simplifying the editor experience does not
remove diagnostic evidence. Known packaging errors are translated into
specific guidance for extra/missing renders, missing project media,
reconstruction mismatch, interrupted state capture, and missing runtime
components. Unknown failures ask the editor to share technical details with
the EditPath team rather than presenting a raw Python exception as an action.

A subsequent acceptance run exercised a speed-changed clip and exposed an MLT
identity edge case: timewarp producers store `resource` as
`speed:/absolute/source` while retaining the original Kdenlive bin ID. Asset
discovery now resolves that representation to the underlying source instead of
reporting two resources for one bin item. Supervisor worker output is appended
to `supervisor-activity.log`, and finalization failures show their concrete last
error in the GUI. GUI-ready acknowledgment is also written beside each ready
signal and the supervisor hides synchronously, making a missed hide observable
and eliminating the delayed-hide race.

## Historical Version 2 manual acceptance test

1. Start a fresh recording and create/open a project.
2. Invoke one menu command and one toolbar action.
3. Invoke a known keyboard shortcut such as undo.
4. Drag a clip on the timeline and trim one edge.
5. Split or delete a clip.
6. Quit normally and validate the JSONL.
7. Confirm the file contains `ui.command`, `ui.shortcut`, `ui.gesture`, and
   `session.end`; semantic events are additional evidence, not the v2 gate.

## Historical Version 2 limitations

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

## Historical Version 2 roadmap

This roadmap is retained as design history. Schema 0.3 and the exact-state
pipeline now implement its identity, snapshot, diff, reconstruction, packaging,
and automated-gate goals; remaining production work is platform distribution,
interactive acceptance, privacy policy, and broader real-project coverage.

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
