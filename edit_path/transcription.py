"""Asynchronous transcription boundary; providers must return literal human speech."""
from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable

from .io import write_json


class TranscriptionWorker:
    def __init__(self, session: Path, provider: Callable[[Path], dict[str, Any]]) -> None:
        self.session, self.provider = session, provider
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="edit-path-transcribe")

    def submit(self, audio_file: Path, metadata: dict[str, Any]) -> Future:
        return self.executor.submit(self._run, audio_file, metadata)

    def _run(self, audio_file: Path, metadata: dict[str, Any]) -> dict[str, Any]:
        try:
            transcript = self.provider(audio_file)
            result = {**metadata, "transcript": transcript}
        except Exception as exc:  # transcription must never endanger capture/editing
            result = {**metadata, "transcription_error": str(exc), "transcript": None}
        out = self.session / "EDIT-PATH" / "reasoning" / f"transcript-{metadata['reasoning_segment_id']}.json"
        write_json(out, result)
        return result

    def close(self) -> None:
        self.executor.shutdown(wait=True, cancel_futures=False)
