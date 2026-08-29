#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Video Path Pilot contributors
# SPDX-License-Identifier: GPL-3.0-only
"""Convert raw recorder evidence into the provisional software-independent sample."""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from edit_path.state import resolve_accepted_branch


ENTITY_PREFIX = {"clip": "clip", "track": "track", "composition": "transition", "mix": "transition", "master_effect": "master"}
COLLECTION = {"clip": "clips", "track": "tracks", "composition": "compositions", "mix": "mixes", "master_effect": "master_effects"}


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def accepted_commits(events: list[dict]) -> list[dict]:
    """Return the final successful branch; raw undo/redo remains in evidence."""
    if any(event.get("event_type") == "state.checkpoint" for event in events):
        return resolve_accepted_branch(
            events,
            require_targets=any(event.get("schema_version") == "0.3.0" for event in events),
        ).accepted
    # Compatibility for early MVP unit fixtures and pre-checkpoint 0.1 logs.
    stack: list[dict] = []
    redo: list[dict] = []
    for event in events:
        if event.get("event_type") != "state.diff":
            continue
        boundary = event.get("boundary")
        if boundary == "commit":
            stack.append(event)
            redo.clear()
        elif boundary == "undo" and stack:
            redo.append(stack.pop())
        elif boundary == "redo" and redo:
            stack.append(redo.pop())
    return stack


def operation_name(diff: dict) -> str:
    changes = diff.get("changes", [])
    entities = {c.get("entity") for c in changes}
    kinds = {c.get("change") for c in changes}
    if entities == {"clip"}:
        if kinds == {"added"}: return "clip.insert"
        if kinds == {"removed"}: return "clip.delete"
        if "removed" in kinds and "updated" in kinds: return "timeline.ripple_delete"
        updated = [c for c in changes if c.get("change") == "updated"]
        fields = set()
        for change in updated:
            before, after = change.get("before", {}), change.get("after", {})
            fields.update(key for key in set(before) | set(after) if before.get(key) != after.get(key))
        if fields <= {"timeline_start_frame", "track_native_id"}: return "clip.move"
        if "speed" in fields: return "clip.speed.change"
        if fields & {"source_start_frame"}: return "clip.trim.in"
        if fields & {"source_end_frame", "duration_frames"}: return "clip.trim.out"
        if fields == {"effects"}:
            before = [e for c in updated for e in c.get("before", {}).get("effects", [])]
            after = [e for c in updated for e in c.get("after", {}).get("effects", [])]
            if len(after) > len(before): return "effect.add"
            if len(after) < len(before): return "effect.remove"
            if {e.get("index") for e in before} != {e.get("index") for e in after}: return "effect.reorder"
            def keyframes(values):
                return [(e.get("id") or e.get("asset_id"), p.get("name"), p.get("value"))
                        for e in values if isinstance(e, dict) for p in e.get("parameters", []) if isinstance(p, dict) and ("=" in str(p.get("value", "")) or "keyframe" in str(p.get("name", "")).lower())]
            bk, ak = keyframes(before), keyframes(after)
            if bk != ak:
                if len(ak) != len(bk): return "keyframe.multi_edit" if abs(len(ak)-len(bk)) > 1 else ("keyframe.add" if len(ak) > len(bk) else "keyframe.remove")
                return "keyframe.value.change"
            return "effect.parameter.change"
    if entities == {"track"}:
        if kinds == {"added"}: return "track.add"
        if "removed" in kinds: return "track.remove"
        fields = {k for c in changes for side in ("before", "after") for k in c.get(side, {})}
        if "name" in fields: return "track.rename"
        if "mute" in fields: return "track.mute"
        if "lock" in fields: return "track.lock"
        return "track.set_state"
    if entities <= {"mix", "composition"}:
        if kinds == {"added"}: return "transition.add"
        if kinds == {"removed"}: return "transition.remove"
        return "transition.parameter.change"
    return "timeline.change"


def event_operation_name(event: dict) -> str:
    """Name every state-changing event without discarding editing history."""
    boundary = event.get("boundary")
    if boundary == "undo":
        return "history.undo"
    if boundary == "redo":
        return "history.redo"
    return operation_name(event.get("diff", {}))


def effect_intent(event: dict) -> dict | None:
    """Extract a stable, human-readable intent from an effect state change."""
    changes = event.get("diff", {}).get("changes", [])
    for change in changes:
        if change.get("entity") != "clip":
            continue
        before = change.get("before", {})
        after = change.get("after", {})
        before_effects = before.get("effects", [])
        after_effects = after.get("effects", [])
        if before_effects == after_effects:
            continue
        return {
            "kind": "effect.change",
            "clip_native_id": change.get("native_id"),
            "before_effects": before_effects,
            "after_effects": after_effects,
            "transaction_id": event.get("transaction_id"),
            "interaction_id": event.get("interaction_id") or event.get("transaction_id"),
            "ambiguous": not bool(event.get("interaction_id")),
        }
    return None


def normalized_change(change: dict, ids: dict[tuple[str, str], str], assets: dict[str, str]) -> dict:
    entity = str(change.get("entity"))
    native = str(change.get("native_id"))
    key = (entity, native)
    if key not in ids:
        recorded_id = change.get("after", {}).get("entity_id") or change.get("before", {}).get("entity_id")
        if recorded_id:
            ids[key] = str(recorded_id)
        else:
            prefix = ENTITY_PREFIX.get(entity, entity)
            ids[key] = f"{prefix}_{sum(1 for e, _ in ids if e == entity) + 1:03d}"

    def clean(value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        result = {k: v for k, v in value.items() if k not in {"native_id", "entity_id"}}
        if "track_native_id" in result:
            track_key = ("track", str(result.pop("track_native_id")))
            if track_key not in ids:
                ids[track_key] = f"track_{sum(1 for e, _ in ids if e == 'track') + 1:03d}"
            result["track_id"] = ids[track_key]
        if "asset_reference" in result:
            ref = str(result.pop("asset_reference"))
            result["asset_id"] = assets.setdefault(ref, f"unresolved_native_asset_{ref}")
        return result

    result = {"change": change.get("change"), "entity_type": entity, "entity_id": ids[key]}
    if "before" in change: result["before"] = clean(change["before"])
    if "after" in change: result["after"] = clean(change["after"])
    return result


def apply_native_diff(snapshot: dict, diff: dict) -> None:
    for change in diff.get("changes", []):
        collection = snapshot.setdefault(COLLECTION[change["entity"]], [])
        position = next((i for i, value in enumerate(collection) if value.get("native_id") == change.get("native_id")), None)
        if change["change"] == "removed" and position is not None:
            collection.pop(position)
        elif change["change"] == "added":
            collection.append(copy.deepcopy(change["after"]))
        elif change["change"] == "updated" and position is not None:
            collection[position] = copy.deepcopy(change["after"])
    if "duration_after" in diff:
        snapshot["duration_frames"] = diff["duration_after"]


def normalized_state(snapshot: dict, ids: dict[tuple[str, str], str], assets: dict[str, str]) -> dict:
    state = {"duration_frames": snapshot.get("duration_frames", 0)}
    for singular, collection_name in COLLECTION.items():
        state[collection_name] = [normalized_change({"entity": singular, "native_id": value.get("native_id"),
                                  "change": "added", "after": value}, ids, assets)["after"] | {
                                  f"{singular}_id": ids[(singular, str(value.get("native_id")))]}
                                  for value in snapshot.get(collection_name, [])]
    return state


def observed_asset_ids(event_groups: list[list[dict]]) -> dict[str, str]:
    """Map native bin references to the recorder's stable asset UUIDs."""
    observed: dict[str, str] = {}

    def observe(value: Any) -> None:
        if not isinstance(value, dict) or value.get("asset_reference") is None or not value.get("asset_id"):
            return
        reference, asset_id = str(value["asset_reference"]), str(value["asset_id"])
        previous = observed.get(reference)
        if previous is not None and previous != asset_id:
            raise ValueError(f"native asset reference {reference} has inconsistent stable asset IDs")
        observed[reference] = asset_id

    for events in event_groups:
        for event in events:
            if event.get("event_type") == "state.checkpoint":
                for clip in event.get("snapshot", {}).get("clips", []):
                    observe(clip)
            elif event.get("event_type") == "state.diff":
                for change in event.get("diff", {}).get("changes", []):
                    observe(change.get("before"))
                    observe(change.get("after"))
    return observed


def build_sample(root: Path, metadata: dict) -> dict:
    raw_artifacts = metadata["artifacts"]["raw_events"]
    if isinstance(raw_artifacts, str):
        raw_artifacts = [{"file": raw_artifacts}]
    event_groups = [read_jsonl(root / artifact["file"]) for artifact in raw_artifacts]
    ids: dict[tuple[str, str], str] = {}
    asset_refs: dict[str, str] = {
        str(asset["bin_reference"]): asset["asset_id"]
        for asset in metadata["assets"]
        if asset.get("bin_reference") is not None
    }
    asset_refs.update({str(key): str(value) for key, value in metadata.get("native_asset_bindings", {}).items()})
    recorded_asset_ids = observed_asset_ids(event_groups)
    embedded_inputs = []
    for reference, descriptor in sorted(metadata.get("embedded_project_assets", {}).items()):
        reference = str(reference)
        if reference not in recorded_asset_ids:
            continue
        asset_id = recorded_asset_ids[reference]
        asset_refs[reference] = asset_id
        embedded_inputs.append({"asset_id": asset_id, **dict(descriptor)})
    timeline_events = [event for events in event_groups for event in events if event.get("event_type") == "state.diff"]
    first_checkpoint = next(
        (event for events in event_groups for event in events if event.get("event_type") == "state.checkpoint"),
        None,
    )
    # Schema 0.3 behavior samples retain the observed undo/redo path. Older
    # recordings had no baseline checkpoint, so preserve their historical
    # accepted-branch normalization instead of making them unreadable.
    normalized_events = timeline_events if first_checkpoint else accepted_commits(
        [event for events in event_groups for event in events]
    )
    operations = []
    for index, event in enumerate(normalized_events, 1):
        diff = event.get("diff", {})
        operation = {
            "operation_id": f"op_{index:04d}",
            "operation": event_operation_name(event),
            "changes": [normalized_change(change, ids, asset_refs) for change in diff.get("changes", [])],
            "resulting_state_hash": event.get("after_hash"),
            "evidence": {"raw_event_id": event.get("event_id"), "raw_sequence": event.get("sequence")},
            "extensions": {"kdenlive": {"command_label": event.get("label"), "boundary": event.get("boundary")}},
        }
        intent = effect_intent(event)
        if intent:
            operation["intent"] = intent
            operation["interaction_id"] = intent["interaction_id"]
        operations.append(operation)
    edit_path = {"time_unit": "frame", "operations": operations}
    if first_checkpoint:
        initial_native = copy.deepcopy(first_checkpoint["snapshot"])
        final_native = copy.deepcopy(initial_native)
        for event in timeline_events:
            apply_native_diff(final_native, event.get("diff", {}))
        edit_path["initial_state"] = normalized_state(initial_native, ids, asset_refs)
        edit_path["final_state"] = normalized_state(final_native, ids, asset_refs)
    input_assets = [{"asset_id": a["asset_id"], "original_filename": a.get("original_filename", Path(a["file"]).name),
                     "file": a["file"], "sha256": a["sha256"], "bytes": a["bytes"]} for a in metadata["assets"]]
    valid_asset_ids = {a["asset_id"] for a in input_assets} | {a["asset_id"] for a in embedded_inputs}
    unresolved = sorted(value for value in set(asset_refs.values()) if value not in valid_asset_ids)
    inputs = {"assets": input_assets}
    if embedded_inputs:
        inputs["embedded_assets"] = embedded_inputs
    return {
        "schema_version": "0.1.0",
        "sample_id": metadata["sample_id"],
        "task": {"prompt": metadata["prompt"]},
        "project": metadata["project"],
        "inputs": inputs,
        "edit_path": edit_path,
        "output": {"video": metadata["artifacts"]["final_video"], "sha256": metadata["artifacts"]["final_video_sha256"]},
        "quality": {
            "raw_session_complete": True,
            "undo_redo_preserved_in_edit_path": first_checkpoint is not None,
            "asset_binding_method": metadata["asset_binding_method"],
            "unresolved_asset_ids": unresolved,
            "review_status": "needs_human_review",
            "output_completion_confirmed": metadata.get("output_completion_confirmed", True),
        },
        "evidence": {
            "raw_events": raw_artifacts,
            "native_project": metadata["artifacts"]["native_project"],
            "native_project_sha256": metadata["artifacts"]["native_project_sha256"],
        },
        "provenance": {"job_id": metadata.get("job_id", metadata["sample_id"]), "collector": "kdenlive-video-path-mvp", "collector_version": "0.2.0"},
    }
