#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Video Path Pilot contributors
# SPDX-License-Identifier: GPL-3.0-only
"""Validate structural invariants of a Video Path pilot JSONL session."""

from __future__ import annotations

import argparse
import json
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


def validate(path: Path) -> list[str]:
    errors: list[str] = []
    session_id: str | None = None
    expected_sequence = 1
    seen_event_ids: set[str] = set()
    event_count = 0

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

            if event.get("schema_version") != "0.1.0":
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
            if event_count == 1 and event_type != "session.start":
                errors.append(f"{prefix}: first event must be session.start")
            if event_count > 1 and event_type == "session.start":
                errors.append(f"{prefix}: duplicate session.start")
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
            elif event_type not in {"session.start", "action", "history"}:
                errors.append(f"{prefix}: unknown event_type {event_type!r}")

    if event_count == 0:
        errors.append("file has no events")
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
