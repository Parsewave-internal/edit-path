"""Best-effort, crash-isolated microphone capture via FFmpeg."""
from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path
from typing import Callable

from .io import write_json


class AudioCapture:
    def __init__(self, session: Path, *, ffmpeg: str = "ffmpeg", segment_seconds: int = 30,
                 popen: Callable = subprocess.Popen) -> None:
        self.session, self.ffmpeg, self.segment_seconds, self._popen = session, ffmpeg, segment_seconds, popen
        self.process: subprocess.Popen | None = None
        self.index = 0

    def command(self, output: Path) -> list[str]:
        if os.name == "nt":
            input_args = ["-f", "dshow", "-i", "audio=default"]
        elif __import__("sys").platform == "darwin":
            input_args = ["-f", "avfoundation", "-i", ":0"]
        else:
            input_args = ["-f", "pulse", "-i", "default"]
        return [self.ffmpeg, "-hide_banner", "-loglevel", "error", *input_args,
                "-t", str(self.segment_seconds), "-c:a", "flac", "-y", str(output)]

    def start_segment(self) -> Path | None:
        if self.process is not None and self.process.poll() is None:
            return None
        self.index += 1
        output = self.session / "EDIT-PATH" / "reasoning" / f"audio-{self.index:03d}.flac"
        output.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.process = self._popen(self.command(output), stdin=subprocess.DEVNULL,
                                       stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            return output
        except OSError as exc:
            write_json(self.session / "EDIT-PATH" / "reasoning" / "capture-error.json",
                       {"schema_version": "edit-path/reasoning-capture@1", "error": str(exc)})
            self.process = None
            return None

    def stop(self) -> None:
        process, self.process = self.process, None
        if process is None:
            return
        try:
            process.terminate()
            process.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            try:
                process.kill()
            except OSError:
                pass
