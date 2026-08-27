# SPDX-FileCopyrightText: 2026 Edit Path contributors
# SPDX-License-Identifier: GPL-3.0-only

from __future__ import annotations

import base64
import copy
import hashlib
import json
import struct
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import EditPathError, GateError
from .io import event_sequence, safe_relative


MAX_STATE_BYTES = 512 * 1024 * 1024
ENTITY_ARRAYS = {
    "track": "tracks",
    "clip": "clips",
    "composition": "compositions",
    "mix": "mixes",
    "master_effect": "master_effects",
}


@dataclass(frozen=True)
class BranchResolution:
    accepted: list[dict[str, Any]]
    baseline_hash: str
    final_hash: str
    state_events: list[dict[str, Any]]


def canonical_bytes(value: dict[str, Any]) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")


def canonical_hash(value: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def _recovery_semantic_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Remove Kdenlive object identities that are regenerated on project load."""
    value = copy.deepcopy(snapshot)
    track_identities = {
        track.get("native_id"): {
            "position": track.get("position"),
            "tag": track.get("tag"),
            "kind": track.get("kind"),
        }
        for track in value.get("tracks", [])
    }

    def normalize(item: Any) -> Any:
        if isinstance(item, dict):
            result = {}
            for key, child in item.items():
                if key in {"native_id", "entity_id"}:
                    continue
                if key == "track_native_id":
                    result["track_identity"] = track_identities.get(child, {"unresolved_track": child})
                else:
                    result[key] = normalize(child)
            return result
        if isinstance(item, list):
            normalized = [normalize(child) for child in item]
            if all(isinstance(child, dict) for child in normalized):
                normalized.sort(key=canonical_bytes)
            return normalized
        return item

    return normalize(value)


def recovery_snapshots_equal(before: dict[str, Any], after: dict[str, Any]) -> bool:
    """Compare reload-boundary states while retaining all editing semantics."""
    return canonical_bytes(_recovery_semantic_snapshot(before)) == canonical_bytes(_recovery_semantic_snapshot(after))


def _sort_snapshot(snapshot: dict[str, Any]) -> None:
    snapshot.setdefault("tracks", []).sort(key=lambda item: (item.get("position", 0), item.get("native_id", 0)))
    snapshot.setdefault("clips", []).sort(
        key=lambda item: (item.get("track_native_id", 0), item.get("timeline_start_frame", 0), item.get("native_id", 0))
    )
    snapshot.setdefault("compositions", []).sort(
        key=lambda item: (item.get("track_native_id", 0), item.get("timeline_start_frame", 0), item.get("native_id", 0))
    )
    snapshot.setdefault("mixes", []).sort(key=lambda item: (item.get("track_native_id", 0), item.get("native_id", 0)))
    snapshot.setdefault("master_effects", []).sort(key=lambda item: item.get("native_id", 0))


def apply_diff(snapshot: dict[str, Any], event: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(snapshot)
    diff = event.get("diff")
    sequence = event_sequence(event)
    if not isinstance(diff, dict):
        raise GateError("state_transition", "state.diff has no diff object", sequence)
    changes = diff.get("changes", [])
    if not isinstance(changes, list):
        raise GateError("state_transition", "state.diff changes is not an array", sequence)
    for change in changes:
        entity = change.get("entity")
        array_name = ENTITY_ARRAYS.get(entity)
        if array_name is None:
            raise GateError("state_transition", f"unsupported diff entity {entity!r}", sequence)
        array = result.setdefault(array_name, [])
        native_id = change.get("native_id")
        indexes = [index for index, item in enumerate(array) if item.get("native_id") == native_id]
        kind = change.get("change")
        if kind == "added":
            if indexes or not isinstance(change.get("after"), dict):
                raise GateError("state_transition", f"invalid add for {entity} {native_id}", sequence)
            array.append(copy.deepcopy(change["after"]))
        elif kind == "removed":
            if len(indexes) != 1 or array[indexes[0]] != change.get("before"):
                raise GateError("state_transition", f"invalid remove for {entity} {native_id}", sequence)
            array.pop(indexes[0])
        elif kind == "updated":
            if len(indexes) != 1 or array[indexes[0]] != change.get("before") or not isinstance(change.get("after"), dict):
                raise GateError("state_transition", f"invalid update for {entity} {native_id}", sequence)
            array[indexes[0]] = copy.deepcopy(change["after"])
        else:
            raise GateError("state_transition", f"unsupported change kind {kind!r}", sequence)
    if "duration_after" in diff:
        result["duration_frames"] = diff["duration_after"]
    _sort_snapshot(result)
    return result


def validate_state_transitions(
    events: list[dict[str, Any]], *, allow_recovery_identity_rebase: bool = True
) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    snapshots: dict[str, dict[str, Any]] = {}
    states: list[dict[str, Any]] = []
    recovery_pending = False
    for event in events:
        event_type = event.get("event_type")
        if event_type == "session.recovered":
            recovery_pending = True
        if event_type not in {"state.checkpoint", "state.diff"}:
            continue
        sequence = event_sequence(event)
        timeline_id = event.get("timeline_id")
        if not isinstance(timeline_id, str) or not timeline_id:
            raise GateError("state_transition", "state event has no timeline_id", sequence)
        if event_type == "state.checkpoint":
            checkpoint = copy.deepcopy(event.get("snapshot"))
            if not isinstance(checkpoint, dict):
                raise GateError("state_transition", "checkpoint snapshot is missing", sequence)
            checkpoint_hash = canonical_hash(checkpoint)
            if checkpoint_hash != event.get("state_hash"):
                raise GateError("state_transition", "checkpoint state hash mismatch", sequence)
            if timeline_id in snapshots and canonical_hash(snapshots[timeline_id]) != checkpoint_hash:
                identity_rebase = (
                    allow_recovery_identity_rebase
                    and recovery_pending
                    and recovery_snapshots_equal(snapshots[timeline_id], checkpoint)
                )
                if not identity_rebase:
                    raise GateError("hash_chain", "checkpoint does not match the reconstructed timeline state", sequence)
            snapshots[timeline_id] = checkpoint
            recovery_pending = False
        else:
            if timeline_id not in snapshots:
                raise GateError("state_transition", "state.diff precedes its timeline checkpoint", sequence)
            before_hash = canonical_hash(snapshots[timeline_id])
            if before_hash != event.get("before_hash"):
                raise GateError("hash_chain", f"before_hash mismatch: expected {before_hash}", sequence)
            snapshots[timeline_id] = apply_diff(snapshots[timeline_id], event)
            after_hash = canonical_hash(snapshots[timeline_id])
            if after_hash != event.get("after_hash"):
                raise GateError("hash_chain", f"after_hash mismatch: reconstructed {after_hash}", sequence)
        states.append({"event": event, "snapshot": copy.deepcopy(snapshots[timeline_id])})
    if not states:
        raise GateError("state_transition", "trajectory contains no state.checkpoint")
    return snapshots, states


def _branch_hash(event: dict[str, Any], which: str) -> str | None:
    value = event.get(f"project_{which}_hash", event.get(f"{which}_hash"))
    return value if isinstance(value, str) else None


def resolve_accepted_branch(events: list[dict[str, Any]], *, require_targets: bool = False) -> BranchResolution:
    checkpoint = next((event for event in events if event.get("event_type") == "state.checkpoint"), None)
    if checkpoint is None or not isinstance(checkpoint.get("state_hash"), str):
        raise GateError("branch_resolution", "state.checkpoint is required before branch resolution")
    project_state = checkpoint.get("project_state")
    if require_targets and (not isinstance(project_state, dict) or not isinstance(project_state.get("sha256"), str)):
        raise GateError("branch_resolution", "v0.3 baseline checkpoint requires an exact project_state hash", event_sequence(checkpoint))
    baseline_hash = project_state.get("sha256") if isinstance(project_state, dict) else checkpoint["state_hash"]
    current_hash = baseline_hash
    accepted: list[dict[str, Any]] = []
    redo: list[list[dict[str, Any]]] = []
    state_events: list[dict[str, Any]] = []
    recovery_pending = False
    epoch_baseline_hash = baseline_hash
    epoch_floor = 0
    for event in events[events.index(checkpoint) + 1 :]:
        if event.get("event_type") == "session.recovered":
            recovery_pending = True
            continue
        if event.get("event_type") == "state.checkpoint" and recovery_pending:
            recovery_state = event.get("project_state")
            recovery_hash = recovery_state.get("sha256") if isinstance(recovery_state, dict) else event.get("state_hash")
            if not isinstance(recovery_hash, str):
                raise GateError("branch_resolution", "recovery checkpoint has no state hash", event_sequence(event))
            current_hash = recovery_hash
            epoch_baseline_hash = recovery_hash
            epoch_floor = len(accepted)
            redo.clear()
            recovery_pending = False
            continue
        if event.get("event_type") != "state.diff":
            continue
        state_events.append(event)
        sequence = event_sequence(event)
        before_hash = _branch_hash(event, "before")
        after_hash = _branch_hash(event, "after")
        if require_targets:
            if not event.get("transaction_id") or not event.get("undo_entry_id"):
                raise GateError("branch_resolution", "v0.3 state.diff requires transaction_id and undo_entry_id", sequence)
            if not isinstance(event.get("project_before_hash"), str) or not isinstance(event.get("project_after_hash"), str):
                raise GateError("branch_resolution", "v0.3 state.diff requires project before/after hashes", sequence)
        if before_hash != current_hash:
            raise GateError("branch_resolution", f"branch before_hash does not equal current state {current_hash}", sequence)
        boundary = event.get("boundary")
        if boundary == "commit":
            accepted.append(event)
            redo.clear()
        elif boundary == "undo":
            if len(accepted) <= epoch_floor:
                raise GateError("branch_resolution", "undo has no accepted transaction to remove", sequence)
            original = accepted[-1]
            target = event.get("target_transaction_id")
            original_id = original.get("transaction_id")
            if require_targets and (not target or not original_id):
                raise GateError("branch_resolution", "v0.3 undo requires target_transaction_id", sequence)
            if target and original_id and target != original_id:
                raise GateError("branch_resolution", "undo targets a transaction other than the accepted stack head", sequence)
            group: list[dict[str, Any]] = []
            while accepted and accepted[-1].get("transaction_id") == original_id:
                group.append(accepted.pop())
                if original_id is None:
                    break
            group.reverse()
            # An undo restores the logical undo-stack position, but Kdenlive
            # does not promise byte-identical project serialization (or even
            # an identical diagnostic semantic snapshot) after doing so.
            # Generated XML properties, effect identities, and other native
            # bookkeeping can legitimately change across an undo.  State and
            # exact-project hash continuity are validated independently; the
            # stable target transaction/undo-entry relationship is the
            # authoritative branch operation here.
            redo.append(group)
        elif boundary == "redo":
            if not redo:
                raise GateError("branch_resolution", "redo has no transaction to restore", sequence)
            group = redo.pop()
            original = group[-1]
            target = event.get("target_transaction_id")
            original_id = original.get("transaction_id")
            if require_targets and (not target or not original_id):
                raise GateError("branch_resolution", "v0.3 redo requires target_transaction_id", sequence)
            if target and original_id and target != original_id:
                raise GateError("branch_resolution", "redo targets the wrong transaction", sequence)
            accepted.extend(group)
        else:
            raise GateError("branch_resolution", f"unsupported boundary {boundary!r}", sequence)
        current_hash = after_hash
    return BranchResolution(accepted, baseline_hash, current_hash, state_events)


def operation_name(diff: dict[str, Any]) -> str:
    changes = diff.get("changes", [])
    entities = {change.get("entity") for change in changes}
    kinds = {change.get("change") for change in changes}
    if entities == {"clip"}:
        if kinds == {"added"}:
            return "clip.insert"
        if kinds == {"removed"}:
            return "clip.delete"
        if "removed" in kinds and "updated" in kinds:
            return "timeline.ripple_delete"
        updated = [change for change in changes if change.get("change") == "updated"]
        fields: set[str] = set()
        for change in updated:
            before, after = change.get("before", {}), change.get("after", {})
            fields.update(key for key in set(before) | set(after) if before.get(key) != after.get(key))
        if fields and fields <= {"timeline_start_frame", "track_native_id"}:
            return "clip.move"
        if "speed" in fields:
            return "clip.set_speed"
        if fields & {"source_start_frame", "source_end_frame", "duration_frames"} or "added" in kinds:
            return "clip.trim_or_split"
        if fields == {"effects"}:
            return "effect.change"
    if entities == {"track"}:
        if kinds == {"added"}:
            return "track.create"
        if "removed" in kinds:
            return "track.delete_or_reorder"
        return "track.set_state"
    if entities and entities <= {"mix", "composition"}:
        if kinds == {"added"}:
            return "transition.add"
        if kinds == {"removed"}:
            return "transition.remove"
        return "transition.change"
    return "timeline.change"


def validate_action_semantics(events: list[dict[str, Any]], accepted: list[dict[str, Any]]) -> list[dict[str, Any]]:
    def compatible(declared: str | None, inferred: str) -> bool:
        return declared is None or declared == inferred or (declared in {"clip.trim", "clip.split"} and inferred == "clip.trim_or_split")

    accepted_by_transaction: dict[str, list[dict[str, Any]]] = {}
    for state_event in accepted:
        transaction_id = state_event.get("transaction_id")
        if isinstance(transaction_id, str):
            accepted_by_transaction.setdefault(transaction_id, []).append(state_event)
    assignments: dict[str, tuple[dict[str, Any], str]] = {}
    ignored_actions: list[dict[str, Any]] = []
    actions = sorted(
        (event for event in events if event.get("event_type") == "action" and event.get("transaction_id")),
        key=event_sequence,
    )
    for action_event in actions:
        transaction_id = str(action_event["transaction_id"])
        declared = action_event.get("action")
        target_events = accepted_by_transaction.get(transaction_id, [])
        if target_events and all(compatible(declared, operation_name(value.get("diff", {}))) for value in target_events):
            assignments[transaction_id] = (action_event, "transaction")
            continue
        # Recorder versions before the post-push fix emitted the semantic
        # action just after QUndoStack had closed its transaction. The next
        # transaction then inherited it. Recover only when a preceding,
        # otherwise-unlinked accepted transaction has matching state semantics.
        candidates = [
            value
            for value in accepted
            if event_sequence(value) < event_sequence(action_event)
            and isinstance(value.get("transaction_id"), str)
            and value["transaction_id"] not in assignments
            and compatible(declared, operation_name(value.get("diff", {})))
        ]
        if not candidates:
            inferred = operation_name(target_events[0].get("diff", {})) if target_events else "unlinked"
            if target_events:
                assignments[transaction_id] = (action_event, "inconsistent_transaction")
            else:
                ignored_actions.append(
                    {
                        "sequence": event_sequence(action_event),
                        "transaction_id": transaction_id,
                        "declared": declared,
                        "inferred": inferred,
                        "linked": False,
                        "compatible": True,
                        "status": "ignored",
                        "attribution": "not_on_accepted_branch",
                    }
                )
            continue
        recovered = max(candidates, key=event_sequence)
        assignments[str(recovered["transaction_id"])] = (action_event, "recovered_post_push")

    reports: list[dict[str, Any]] = []
    for event in accepted:
        inferred = operation_name(event.get("diff", {}))
        transaction_id = event.get("transaction_id")
        assignment = assignments.get(transaction_id)
        action_event = assignment[0] if assignment else None
        declared = action_event.get("action") if action_event else None
        is_compatible = compatible(declared, inferred)
        reports.append({
            "sequence": event_sequence(event),
            "transaction_id": transaction_id,
            "declared": declared,
            "inferred": inferred,
            "linked": action_event is not None,
            "compatible": is_compatible,
            "status": "passed" if is_compatible else "degraded",
            "attribution": assignment[1] if assignment else "state_only",
        })
    return [*reports, *ignored_actions]


def load_state_reference(reference: dict[str, Any], base_dir: Path) -> bytes:
    encoding = reference.get("encoding", "raw")
    if encoding == "qt-qcompress-base64" and "data" in reference:
        try:
            encoded = base64.b64decode(reference["data"], validate=True)
        except (ValueError, TypeError, KeyError) as exc:
            raise EditPathError(f"invalid base64 project state: {exc}") from exc
        return _decode_qcompress_bytes(encoded, reference)
    relative = safe_relative(str(reference.get("path", "")))
    path = base_dir / relative
    if not path.is_file() or path.is_symlink():
        raise EditPathError(f"project state sidecar is missing: {relative}")
    encoded = path.read_bytes()
    if encoding == "raw":
        raw = encoded
    elif encoding == "zstd":
        try:
            import zstandard
        except ImportError as exc:
            raise EditPathError("zstandard is required to decode project state sidecars") from exc
        raw = zstandard.ZstdDecompressor().decompress(encoded, max_output_size=MAX_STATE_BYTES)
    elif encoding == "qt-qcompress":
        if len(encoded) < 5:
            raise EditPathError("qt-qcompress state is truncated")
        expected = struct.unpack(">I", encoded[:4])[0]
        if expected > MAX_STATE_BYTES:
            raise EditPathError("project state exceeds the decompression limit")
        raw = zlib.decompress(encoded[4:])
        if len(raw) != expected:
            raise EditPathError("qt-qcompress state size mismatch")
    elif encoding == "qt-qcompress-base64":
        return _decode_qcompress_bytes(encoded, reference)
    else:
        raise EditPathError(f"unsupported project state encoding: {encoding!r}")
    if len(raw) != reference.get("bytes"):
        raise EditPathError("project state byte count mismatch")
    if hashlib.sha256(raw).hexdigest() != reference.get("sha256"):
        raise EditPathError("project state SHA-256 mismatch")
    return raw


def _decode_qcompress_bytes(encoded: bytes, reference: dict[str, Any]) -> bytes:
    if len(encoded) < 5:
        raise EditPathError("qt-qcompress state is truncated")
    expected = struct.unpack(">I", encoded[:4])[0]
    if expected > MAX_STATE_BYTES:
        raise EditPathError("project state exceeds the decompression limit")
    raw = zlib.decompress(encoded[4:])
    if len(raw) != expected or len(raw) != reference.get("bytes"):
        raise EditPathError("project state byte count mismatch")
    if hashlib.sha256(raw).hexdigest() != reference.get("sha256"):
        raise EditPathError("project state SHA-256 mismatch")
    return raw
