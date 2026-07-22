# SPDX-FileCopyrightText: 2026 Edit Path contributors
# SPDX-License-Identifier: GPL-3.0-only

from __future__ import annotations

import copy
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .errors import EditPathError, GateError
from .io import read_jsonl, write_jsonl
from .pipeline import validate_event_envelope
from .state import validate_state_transitions


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def discover_segments(session_dir: Path) -> list[Path]:
    session_dir = session_dir.expanduser().resolve()
    paths = sorted(session_dir.glob("raw-events-*.jsonl"))
    if not paths and (session_dir / "raw-events.jsonl").is_file():
        paths = [session_dir / "raw-events.jsonl"]
    if not paths:
        raise EditPathError(f"session contains no raw event segments: {session_dir}")
    return paths


def assemble_segments(
    session_dir: Path,
    output: Path | None = None,
    *,
    session_id: str | None = None,
) -> dict[str, Any]:
    """Assemble crash-recovery segments into one validated canonical trajectory.

    Raw segments remain immutable evidence. The assembled trajectory has one
    session envelope, one project context, contiguous sequence numbers, and an
    explicit recovery event before every resumed segment.
    """

    session_dir = session_dir.expanduser().resolve()
    paths = discover_segments(session_dir)
    groups = [read_jsonl(path) for path in paths]
    schema_versions = {str(event.get("schema_version")) for events in groups for event in events}
    if len(schema_versions) != 1:
        raise GateError("segment_assembly", f"recovery segments mix schema versions: {sorted(schema_versions)}")
    schema_version = next(iter(schema_versions))

    original_session_ids: list[str] = []
    for index, events in enumerate(groups):
        envelope = validate_event_envelope(events, require_complete=index == len(groups) - 1)
        original_session_ids.append(str(envelope["session_id"]))
        if index < len(groups) - 1 and events[-1].get("event_type") == "session.end":
            raise GateError("segment_assembly", f"non-final segment ended normally: {paths[index].name}")
        state_events = [event for event in events if event.get("event_type") in {"state.checkpoint", "state.diff"}]
        if state_events:
            if state_events[0].get("event_type") != "state.checkpoint":
                raise GateError("segment_assembly", f"segment starts state capture without a checkpoint: {paths[index].name}")
            validate_state_transitions(events)

    canonical_session_id = session_id or original_session_ids[0]
    assembled: list[dict[str, Any]] = []
    project_context_seen = False
    for segment_index, (path, events) in enumerate(zip(paths, groups), 1):
        if segment_index > 1:
            assembled.append(
                {
                    "schema_version": schema_version,
                    "session_id": canonical_session_id,
                    "event_id": str(uuid.uuid4()),
                    "timestamp_utc": events[0].get("timestamp_utc") or _utc_now(),
                    "event_type": "session.recovered",
                    "reason": "collector.recovery_segment",
                    "details": {
                        "segment": segment_index,
                        "source": path.name,
                        "original_session_id": original_session_ids[segment_index - 1],
                    },
                }
            )
        for event in events:
            event_type = event.get("event_type")
            if segment_index > 1 and event_type == "session.start":
                continue
            if event_type == "project.context":
                if project_context_seen:
                    continue
                project_context_seen = True
            value = copy.deepcopy(event)
            value["session_id"] = canonical_session_id
            assembled.append(value)

    for sequence, event in enumerate(assembled, 1):
        event["sequence"] = sequence

    validate_event_envelope(assembled)
    validate_state_transitions(assembled)
    destination = (output or session_dir / "trajectory.jsonl").expanduser().resolve()
    write_jsonl(destination, assembled)
    return {
        "path": str(destination),
        "session_id": canonical_session_id,
        "schema_version": schema_version,
        "segments": len(paths),
        "events": len(assembled),
        "original_session_ids": original_session_ids,
    }
