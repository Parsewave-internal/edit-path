"""Optional offline Whisper CLI provider.

The provider returns Whisper's literal JSON result unchanged. It never turns
spoken explanations into inferred editor intent.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any
from .diagnostics import log_event, log_exception


def transcribe_with_whisper(audio_file: Path, *, model: str | None = None,
                            whisper_binary: str | None = None,
                            language: str | None = None) -> dict[str, Any]:
    session = audio_file.parents[3] if len(audio_file.parents) >= 4 else audio_file.parent
    log_event(session, "transcription_start", audio_file=str(audio_file), model=model or os.environ.get("EDIT_PATH_WHISPER_MODEL", "turbo"))
    if not audio_file.is_file():
        log_event(session, "transcription_rejected", reason="audio_missing")
        raise FileNotFoundError(audio_file)
    binary = whisper_binary or os.environ.get("EDIT_PATH_WHISPER_BIN") or shutil.which("whisper")
    if not binary:
        log_event(session, "transcription_unavailable", reason="whisper_not_on_path")
        raise RuntimeError("offline Whisper is not installed; install openai-whisper and expose whisper on PATH")
    selected_model = model or os.environ.get("EDIT_PATH_WHISPER_MODEL", "turbo")
    with tempfile.TemporaryDirectory(prefix="edit-path-whisper-") as directory:
        command = [binary, str(audio_file), "--model", selected_model,
                   "--output_format", "json", "--output_dir", directory,
                   "--verbose", "False"]
        if language:
            command.extend(["--language", language])
        completed = subprocess.run(command, text=True, capture_output=True)
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout)[-4000:]
            log_event(session, "transcription_failed", returncode=completed.returncode, detail=detail)
            raise RuntimeError(f"Whisper failed ({completed.returncode}): {detail}")
        result_path = Path(directory) / f"{audio_file.stem}.json"
        if not result_path.is_file():
            raise RuntimeError("Whisper completed without producing a JSON transcript")
        result = json.loads(result_path.read_text(encoding="utf-8"))
        if not isinstance(result, dict) or not isinstance(result.get("text"), str):
            raise RuntimeError("Whisper JSON transcript has no literal text field")
        log_event(session, "transcription_complete", text_length=len(result["text"]))
        return result
