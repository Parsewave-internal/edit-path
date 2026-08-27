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


def transcribe_with_whisper(audio_file: Path, *, model: str | None = None,
                            whisper_binary: str | None = None,
                            language: str | None = None) -> dict[str, Any]:
    if not audio_file.is_file():
        raise FileNotFoundError(audio_file)
    binary = whisper_binary or os.environ.get("EDIT_PATH_WHISPER_BIN") or shutil.which("whisper")
    if not binary:
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
            raise RuntimeError(f"Whisper failed ({completed.returncode}): {detail}")
        result_path = Path(directory) / f"{audio_file.stem}.json"
        if not result_path.is_file():
            raise RuntimeError("Whisper completed without producing a JSON transcript")
        result = json.loads(result_path.read_text(encoding="utf-8"))
        if not isinstance(result, dict) or not isinstance(result.get("text"), str):
            raise RuntimeError("Whisper JSON transcript has no literal text field")
        return result
