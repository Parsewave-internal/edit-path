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
| `clip.trim_or_split` | Change source bounds, duration, or create split instances. |
| `clip.delete` | Remove clip instances without closing a downstream gap. |
| `clip.set_speed` | Change playback rate and resulting timing. |
| `timeline.ripple_delete` | Remove material and move downstream instances to close the gap. |
| `effect.change` | Add, remove, enable, parameterize, or keyframe an effect. |
| `transition.add` / `transition.remove` / `transition.change` | Change a mix or transition between clips. |
| `track.create` / `track.delete_or_reorder` / `track.set_state` | Change track structure, ordering, mute, visibility, or lock. |
| `timeline.change` | Review-required fallback when the MVP cannot safely classify a state diff. |

`clip.trim_or_split`, `effect.change`, and the track fallback names are wider
than the eventual production vocabulary. The raw before/after state makes them
lossless while two real samples reveal which distinctions are dependable.

## Identity and asset binding

Canonical IDs (`asset_001`, `clip_001`, `track_001`, …) are local to a sample.
The MVP maps Kdenlive bin references to assets by first use, so editors must
import the copied files from `assets/` in filename order. This assumption is
recorded in `quality.asset_binding_method` and must be replaced by persistent
UUIDs or project-file resolution before scaled collection.

## Undo and redo

Undo and redo are preserved chronologically in `edit_path.operations` as
`history.undo` and `history.redo`. Each contains the actual reverse or restored
state change and resulting state hash. Raw UI/history evidence also remains in
`evidence/raw-events*.jsonl`. The MVP intentionally retains more information;
a final-branch-only view can be derived later without recollecting data.
