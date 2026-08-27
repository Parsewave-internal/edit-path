"""Finalize captured reasoning audio into literal transcripts and captions."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

from .io import write_json
from .reasoning import transcript_to_vtt
from .transcription import TranscriptionWorker


def transcribe_reasoning_segments(session: Path, segments: list[Path], *,
                                  provider: Callable[[Path], dict[str, Any]],
                                  metadata: dict[str, Any] | None = None) -> dict[str, Any]:
    """Transcribe segments off-thread and emit one deterministic captions.vtt."""
    worker = TranscriptionWorker(session, provider)
    futures = [worker.submit(path, {**(metadata or {}), "reasoning_segment_id": path.stem}) for path in segments]
    records = [future.result() for future in futures]
    worker.close()
    reasoning_dir = session / "EDIT-PATH" / "reasoning"
    captions = transcript_to_vtt(records)
    (reasoning_dir / "captions.vtt").write_text(captions, encoding="utf-8")
    return {"segments": len(records), "transcribed": sum(r.get("transcript") is not None for r in records),
            "captions": str((reasoning_dir / "captions.vtt").relative_to(session)), "records": records}
