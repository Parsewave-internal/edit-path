"""Session controller used by the Kdenlive reasoning-record toggle."""
from __future__ import annotations

import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .audio_capture import AudioCapture
from .reasoning import append_reasoning_record


class ReasoningSession:
    def __init__(self, session: Path, *, session_id: str, capture: AudioCapture | None = None) -> None:
        self.session, self.session_id = session, session_id
        self.capture = capture or AudioCapture(session)
        self.started_ns: int | None = None
        self.audio_file: Path | None = None

    @property
    def active(self) -> bool:
        return self.started_ns is not None

    def start(self) -> bool:
        if self.active:
            return False
        self.started_ns = time.monotonic_ns()
        self.audio_file = self.capture.start_segment()
        if self.audio_file is None:
            self.started_ns = None
            return False
        return True

    def stop(self, *, events: list[dict[str, Any]] | None = None, timeline_frame_start: int | None = None,
             timeline_frame_end: int | None = None) -> dict[str, Any] | None:
        if not self.active:
            return None
        ended_ns = time.monotonic_ns()
        self.capture.stop()
        record = {
            "schema_version": "edit-path/reasoning@1",
            "session_id": self.session_id,
            "reasoning_segment_id": f"reasoning-{ended_ns}",
            "audio_file": str(self.audio_file.relative_to(self.session)).replace("\\", "/") if self.audio_file else None,
            "started_monotonic_ns": self.started_ns,
            "ended_monotonic_ns": ended_ns,
            "started_utc": datetime.now(timezone.utc).isoformat(),
            "ended_utc": datetime.now(timezone.utc).isoformat(),
            "timeline_frame_start": timeline_frame_start,
            "timeline_frame_end": timeline_frame_end,
        }
        if events is not None:
            from .reasoning import align_reasoning
            record = align_reasoning(record, events)
        append_reasoning_record(self.session, record)
        self.started_ns = None
        self.audio_file = None
        return record
