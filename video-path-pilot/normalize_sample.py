#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Video Path Pilot contributors
# SPDX-License-Identifier: GPL-3.0-only
"""Convert raw recorder evidence into the provisional software-independent sample."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


ENTITY_PREFIX = {"clip": "clip", "track": "track", "composition": "transition", "mix": "transition", "master_effect": "master"}


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
            result["asset_id"] = assets.setdefault(ref, f"asset_{len(assets) + 1:03d}")
        return result

    result = {"change": change.get("change"), "entity_type": entity, "entity_id": ids[key]}
    if "before" in change: result["before"] = clean(change["before"])
    if "after" in change: result["after"] = clean(change["after"])
    return result


def build_sample(root: Path, metadata: dict) -> dict:
    events = read_jsonl(root / "evidence" / "raw-events.jsonl")
    ids: dict[tuple[str, str], str] = {}
    asset_refs: dict[str, str] = {}
    operations = []
    for index, event in enumerate(accepted_commits(events), 1):
        diff = event.get("diff", {})
        operations.append({
            "operation_id": f"op_{index:04d}",
            "operation": operation_name(diff),
            "changes": [normalized_change(change, ids, asset_refs) for change in diff.get("changes", [])],
            "resulting_state_hash": event.get("after_hash"),
            "evidence": {"raw_event_id": event.get("event_id"), "raw_sequence": event.get("sequence")},
            "extensions": {"kdenlive": {"command_label": event.get("label")}},
        })
    notes = read_jsonl(root / "internal" / "rationale.jsonl")
    # Notes are entered immediately after a meaningful decision. Associate each
    # with the latest accepted edit that had completed when the note was saved.
    for note in notes:
        preceding = [
            (operation, event) for operation, event in zip(operations, accepted_commits(events))
            if event.get("timestamp_utc", "") <= note.get("timestamp_utc", "")
        ]
        note["after_operation_id"] = preceding[-1][0]["operation_id"] if preceding else None
    notes_by_operation: dict[str, list[str]] = {}
    for note in notes:
        if note.get("after_operation_id"):
            notes_by_operation.setdefault(note["after_operation_id"], []).append(note.get("note_id"))
    for operation in operations:
        operation["rationale_note_ids"] = notes_by_operation.get(operation["operation_id"], [])
    input_assets = [{k: a[k] for k in ("asset_id", "original_filename", "file", "sha256", "bytes")} for a in metadata["assets"]]
    unresolved = sorted(set(asset_refs.values()) - {a["asset_id"] for a in input_assets})
    return {
        "schema_version": "0.1.0",
        "sample_id": metadata["sample_id"],
        "task": {"prompt": metadata["prompt"], "editor_plan": metadata["editor_plan"]},
        "project": metadata["project"],
        "inputs": {"assets": input_assets},
        "edit_path": {"time_unit": "frame", "operations": operations},
        "rationale": {"decision_notes": notes, "editor_review": metadata["editor_review"]},
        "output": {"video": metadata["artifacts"]["final_video"], "sha256": metadata["artifacts"]["final_video_sha256"]},
        "quality": {
            "raw_session_complete": True,
            "undo_redo_removed_from_edit_path": True,
            "asset_binding_method": metadata["asset_binding_method"],
            "unresolved_asset_ids": unresolved,
            "review_status": "needs_human_review",
        },
        "evidence": {
            "raw_events": metadata["artifacts"]["raw_events"],
            "raw_events_sha256": metadata["artifacts"]["raw_events_sha256"],
            "native_project": metadata["artifacts"]["native_project"],
            "native_project_sha256": metadata["artifacts"]["native_project_sha256"],
        },
        "provenance": {"editor_id": metadata["editor"]["editor_id"], "collector": "kdenlive-video-path-mvp", "collector_version": "0.1.0"},
    }
