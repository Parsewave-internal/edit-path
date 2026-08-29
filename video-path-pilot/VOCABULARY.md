<!-- SPDX-FileCopyrightText: 2026 Video Path Pilot contributors -->
<!-- SPDX-License-Identifier: GPL-3.0-only -->

# Provisional software-independent vocabulary

This vocabulary describes editing outcomes, not Kdenlive menus or mouse
coordinates. It is deliberately provisional until the client reviews two real
samples. Every operation uses integer frames and ends with a canonical state
hash. Native application details belong only under `extensions`.

## MVP operations

| Operation | Meaning |
|---|---|
| `clip.insert` | Place one or more asset-derived clip instances on tracks. |
| `clip.move` | Change timeline start and/or destination track. |
| `clip.trim.in` / `clip.trim.out` | Change the source in-point or out-point/duration. Split instances remain represented by their added/updated clip changes. |
| `clip.delete` | Remove clip instances without closing a downstream gap. |
| `clip.speed.change` | Change playback rate and resulting timing. |
| `timeline.ripple_delete` | Remove material and move downstream instances to close the gap. |
| `effect.add` / `effect.remove` / `effect.reorder` / `effect.parameter.change` | Change an effect stack or a non-keyframed parameter. |
| `keyframe.add` / `keyframe.remove` / `keyframe.multi_edit` / `keyframe.value.change` | Add, remove, or change one or more keyframed values. |
| `transition.add` / `transition.remove` / `transition.parameter.change` | Change a mix or transition between clips. |
| `track.add` / `track.remove` / `track.rename` / `track.mute` / `track.lock` / `track.set_state` | Change track structure, naming, mute, visibility, or lock. |
| `timeline.change` | Review-required fallback when the MVP cannot safely classify a state diff. |

The raw before/after state remains lossless, and `timeline.change` is retained
when a diff does not meet a specific classifier contract. Native application
details remain under `extensions` and are never substituted for the canonical
operation name.

## Identity and asset binding

Canonical IDs (`asset_001`, `clip_001`, `track_001`, …) are local to a sample.
The MVP resolves Kdenlive project resources by content hash and records native
bin-ID bindings separately under `provenance/asset-bindings.json`. Editors may
import media in any order and may discover or create assets during the edit.

## Undo and redo

Undo and redo are preserved chronologically in `edit_path.operations` as
`history.undo` and `history.redo`. Each contains the actual reverse or restored
state change and resulting state hash. Raw UI/history evidence also remains in
`provenance/segments/raw-events-*.jsonl`. The MVP intentionally retains more information;
a final-branch-only view can be derived later without recollecting data.
