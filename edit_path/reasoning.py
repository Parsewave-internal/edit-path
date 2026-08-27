"""Crash-safe editor think-aloud journal primitives.

Audio capture and transcription providers are deliberately kept outside this
module.  This layer only persists provenance and aligns transcript spans with
the canonical event journal; it never invents editor intent.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable

from .io import replace_with_retry


def reasoning_root(session: Path) -> Path:
    root = session / "EDIT-PATH" / "reasoning"
    root.mkdir(parents=True, exist_ok=True)
    return root


def append_reasoning_record(session: Path, record: dict[str, Any]) -> None:
    """Atomically append a reasoning record, preserving it across crashes."""
    path = session / "EDIT-PATH" / "reasoning-events.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    existing = temporary.read_text(encoding="utf-8") if temporary.exists() else ""
    with temporary.open("w", encoding="utf-8") as stream:
        if path.exists():
            stream.write(path.read_text(encoding="utf-8"))
        stream.write(json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n")
        stream.flush()
        import os
        os.fsync(stream.fileno())
    replace_with_retry(temporary, path)


def align_reasoning(record: dict[str, Any], events: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Attach overlapping/nearest event IDs using monotonic timestamps."""
    start = int(record["started_monotonic_ns"])
    end = int(record["ended_monotonic_ns"])
    ordered = sorted(events, key=lambda e: int(e.get("monotonic_ns", e.get("timestamp_monotonic_ns", 0))))
    times = [int(e.get("monotonic_ns", e.get("timestamp_monotonic_ns", 0))) for e in ordered]
    overlaps = [e for e, t in zip(ordered, times) if start <= t <= end]
    before = [e for e, t in zip(ordered, times) if t < start]
    after = [e for e, t in zip(ordered, times) if t > end]
    result = dict(record)
    result["overlapping_event_ids"] = [e.get("event_id") for e in overlaps if e.get("event_id")]
    result["previous_event_id"] = before[-1].get("event_id") if before else None
    result["next_event_id"] = after[0].get("event_id") if after else None
    return result
