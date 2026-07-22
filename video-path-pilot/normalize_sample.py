#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Video Path Pilot contributors
# SPDX-License-Identifier: GPL-3.0-only
"""Convert raw recorder evidence into the provisional software-independent sample."""

from __future__ import annotations

import json
import copy
from pathlib import Path
from typing import Any


ENTITY_PREFIX = {"clip": "clip", "track": "track", "composition": "transition", "mix": "transition", "master_effect": "master"}
COLLECTION = {"clip": "clips", "track": "tracks", "composition": "compositions", "mix": "mixes", "master_effect": "master_effects"}


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def accepted_commits(events: list[dict]) -> list[dict]:
    """Return the final successful branch; raw undo/redo remains in evidence."""
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
        if "speed" in fields: return "clip.set_speed"
        if fields & {"source_start_frame", "source_end_frame", "duration_frames"}: return "clip.trim_or_split"
        if fields == {"effects"}: return "effect.change"
    if entities == {"track"}:
        if kinds == {"added"}: return "track.create"
        if "removed" in kinds: return "track.delete_or_reorder"
        return "track.set_state"
    if entities <= {"mix", "composition"}:
        if kinds == {"added"}: return "transition.add"
        if kinds == {"removed"}: return "transition.remove"
        return "transition.change"
    return "timeline.change"


def normalized_change(change: dict, ids: dict[tuple[str, str], str], assets: dict[str, str]) -> dict:
    entity = str(change.get("entity"))
    native = str(change.get("native_id"))
    key = (entity, native)
    if key not in ids:
        prefix = ENTITY_PREFIX.get(entity, entity)
        ids[key] = f"{prefix}_{sum(1 for e, _ in ids if e == entity) + 1:03d}"

    def clean(value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        result = {k: v for k, v in value.items() if k != "native_id"}
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


def build_sample(root: Path, metadata: dict) -> dict:
    raw_artifacts = metadata["artifacts"]["raw_events"]
    if isinstance(raw_artifacts, str):
        raw_artifacts = [{"file": raw_artifacts}]
    event_groups = [read_jsonl(root / artifact["file"]) for artifact in raw_artifacts]
    ids: dict[tuple[str, str], str] = {}
    asset_refs: dict[str, str] = dict(metadata.get("native_asset_bindings", {}))
    operations = []
    commits = [event for events in event_groups for event in accepted_commits(events)]
    for index, event in enumerate(commits, 1):
        diff = event.get("diff", {})
        operations.append({
            "operation_id": f"op_{index:04d}",
            "operation": operation_name(diff),
            "changes": [normalized_change(change, ids, asset_refs) for change in diff.get("changes", [])],
            "resulting_state_hash": event.get("after_hash"),
            "evidence": {"raw_event_id": event.get("event_id"), "raw_sequence": event.get("sequence")},
            "extensions": {"kdenlive": {"command_label": event.get("label")}},
        })
    first_checkpoint = next((event for event in event_groups[0] if event.get("event_type") == "state.checkpoint"), None)
    if not first_checkpoint:
        raise ValueError("recording has no canonical checkpoint")
    initial_native = copy.deepcopy(first_checkpoint["snapshot"])
    final_native = copy.deepcopy(initial_native)
    for event in commits:
        apply_native_diff(final_native, event.get("diff", {}))
    initial_state = normalized_state(initial_native, ids, asset_refs)
    final_state = normalized_state(final_native, ids, asset_refs)
    input_assets = [{"asset_id": a["asset_id"], "original_filename": a.get("original_filename", Path(a["file"]).name),
                     "file": a["file"], "sha256": a["sha256"], "bytes": a["bytes"]} for a in metadata["assets"]]
    valid_asset_ids = {a["asset_id"] for a in input_assets}
    unresolved = sorted(value for value in set(asset_refs.values()) if value not in valid_asset_ids)
    return {
        "schema_version": "0.1.0",
        "sample_id": metadata["sample_id"],
        "task": {"prompt": metadata["prompt"]},
        "project": metadata["project"],
        "inputs": {"assets": input_assets},
        "edit_path": {"time_unit": "frame", "initial_state": initial_state, "operations": operations, "final_state": final_state},
        "output": {"video": metadata["artifacts"]["final_video"], "sha256": metadata["artifacts"]["final_video_sha256"]},
        "quality": {
            "raw_session_complete": True,
            "undo_redo_removed_from_edit_path": True,
            "asset_binding_method": metadata["asset_binding_method"],
            "unresolved_asset_ids": unresolved,
            "review_status": "needs_human_review",
            "output_completion_confirmed": metadata["output_completion_confirmed"],
        },
        "evidence": {
            "raw_events": raw_artifacts,
            "native_project": metadata["artifacts"]["native_project"],
            "native_project_sha256": metadata["artifacts"]["native_project_sha256"],
        },
        "provenance": {"job_id": metadata.get("job_id", metadata["sample_id"]), "collector": "kdenlive-video-path-mvp", "collector_version": "0.2.0"},
    }
