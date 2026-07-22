#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Video Path Pilot contributors
# SPDX-License-Identifier: GPL-3.0-only
"""Validate structural invariants of a Video Path pilot JSONL session."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path


ALLOWED_ACTIONS = {
    "clip.insert",
    "clip.move",
    "clip.trim",
    "clip.split",
    "clip.delete",
    "history.undo",
    "history.redo",
}

ALLOWED_EVENT_TYPES = {
    "session.start",
    "session.end",
    "action",
    "history",
    "ui.command",
    "ui.shortcut",
    "ui.gesture",
    "state.checkpoint",
    "state.diff",
}


def validate(path: Path, require_complete: bool = True) -> list[str]:
    errors: list[str] = []
    session_id: str | None = None
    expected_sequence = 1
    seen_event_ids: set[str] = set()
    event_count = 0
    session_end_count = 0
    last_event_type: str | None = None
    timeline_hashes: dict[str, str] = {}

    with path.open("r", encoding="utf-8") as stream:
        for line_number, raw_line in enumerate(stream, 1):
            if not raw_line.strip():
                continue
            event_count += 1
            try:
                event = json.loads(raw_line)
            except json.JSONDecodeError as exc:
                errors.append(f"line {line_number}: invalid JSON: {exc}")
                continue

            prefix = f"line {line_number}"
            for field in (
                "schema_version",
                "session_id",
                "sequence",
                "event_id",
                "timestamp_utc",
                "event_type",
            ):
                if field not in event:
                    errors.append(f"{prefix}: missing {field}")

            if event.get("schema_version") not in {"0.1.0", "0.2.0"}:
                errors.append(f"{prefix}: unsupported schema_version")
            if session_id is None:
                session_id = event.get("session_id")
            elif event.get("session_id") != session_id:
                errors.append(f"{prefix}: session_id changed")
            if event.get("sequence") != expected_sequence:
                errors.append(
                    f"{prefix}: expected sequence {expected_sequence}, "
                    f"got {event.get('sequence')!r}"
                )
            expected_sequence += 1

            event_id = event.get("event_id")
            if event_id in seen_event_ids:
                errors.append(f"{prefix}: duplicate event_id {event_id!r}")
            if isinstance(event_id, str):
                seen_event_ids.add(event_id)

            event_type = event.get("event_type")
            last_event_type = event_type
            if event_count == 1 and event_type != "session.start":
                errors.append(f"{prefix}: first event must be session.start")
            if event_count > 1 and event_type == "session.start":
                errors.append(f"{prefix}: duplicate session.start")
            if event_type not in ALLOWED_EVENT_TYPES:
                errors.append(f"{prefix}: unknown event_type {event_type!r}")
            if event_type == "session.end":
                session_end_count += 1
                if session_end_count > 1:
                    errors.append(f"{prefix}: duplicate session.end")
            if event_type in {"action", "history"}:
                action = event.get("action")
                if action not in ALLOWED_ACTIONS:
                    errors.append(f"{prefix}: unknown action {action!r}")
            if event_type == "action":
                if not isinstance(event.get("timeline_id"), str):
                    errors.append(f"{prefix}: action requires timeline_id")
                if not isinstance(event.get("parameters"), dict):
                    errors.append(f"{prefix}: action requires parameters object")
            elif event_type == "history" and not isinstance(event.get("label"), str):
                errors.append(f"{prefix}: history event requires label")
            elif event_type == "ui.command":
                for field in ("interaction_id", "command_id", "label", "source"):
                    if not isinstance(event.get(field), str):
                        errors.append(f"{prefix}: ui.command requires string {field}")
                if not isinstance(event.get("shortcuts"), list):
                    errors.append(f"{prefix}: ui.command requires shortcuts array")
            elif event_type == "ui.shortcut":
                if not isinstance(event.get("interaction_id"), str):
                    errors.append(f"{prefix}: ui.shortcut requires interaction_id")
                if not isinstance(event.get("key_sequence"), str):
                    errors.append(f"{prefix}: ui.shortcut requires key_sequence")
            elif event_type == "ui.gesture":
                for field in ("interaction_id", "gesture", "target"):
                    if not isinstance(event.get(field), str):
                        errors.append(f"{prefix}: ui.gesture requires string {field}")
                for field in ("start_global", "end_global"):
                    point = event.get(field)
                    if not isinstance(point, dict) or not all(isinstance(point.get(axis), (int, float)) for axis in ("x", "y")):
                        errors.append(f"{prefix}: ui.gesture requires numeric {field}.x/y")
            elif event_type == "state.checkpoint":
                for field in ("timeline_id", "label", "state_hash"):
                    if not isinstance(event.get(field), str):
                        errors.append(f"{prefix}: state.checkpoint requires string {field}")
                if not isinstance(event.get("snapshot"), dict):
                    errors.append(f"{prefix}: state.checkpoint requires snapshot object")
                timeline_id = event.get("timeline_id")
                state_hash = event.get("state_hash")
                if isinstance(state_hash, str) and not re.fullmatch(r"[0-9a-f]{64}", state_hash):
                    errors.append(f"{prefix}: invalid state_hash")
                if isinstance(timeline_id, str) and isinstance(state_hash, str):
                    timeline_hashes[timeline_id] = state_hash
            elif event_type == "state.diff":
                for field in ("timeline_id", "label", "boundary", "before_hash", "after_hash"):
                    if not isinstance(event.get(field), str):
                        errors.append(f"{prefix}: state.diff requires string {field}")
                if event.get("boundary") not in {"commit", "undo", "redo"}:
                    errors.append(f"{prefix}: invalid state.diff boundary")
                if not isinstance(event.get("diff"), dict):
                    errors.append(f"{prefix}: state.diff requires diff object")
                timeline_id = event.get("timeline_id")
                before_hash = event.get("before_hash")
                after_hash = event.get("after_hash")
                for field, value in (("before_hash", before_hash), ("after_hash", after_hash)):
                    if isinstance(value, str) and not re.fullmatch(r"[0-9a-f]{64}", value):
                        errors.append(f"{prefix}: invalid {field}")
                if isinstance(timeline_id, str) and timeline_id in timeline_hashes and before_hash != timeline_hashes[timeline_id]:
                    errors.append(f"{prefix}: before_hash does not continue timeline hash chain")
                if isinstance(timeline_id, str) and isinstance(after_hash, str):
                    timeline_hashes[timeline_id] = after_hash

    if event_count == 0:
        errors.append("file has no events")
    elif require_complete and session_end_count == 0:
        errors.append("incomplete session: missing session.end (application may have crashed or been force-quit)")
    elif require_complete and last_event_type != "session.end":
        errors.append("session.end must be the final event")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("session", type=Path)
    args = parser.parse_args()
    try:
        errors = validate(args.session)
    except OSError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if errors:
        for error in errors:
            print(error, file=sys.stderr)
        return 1
    print(f"valid video-path session: {args.session}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
