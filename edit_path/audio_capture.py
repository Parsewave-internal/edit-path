"""Best-effort, crash-isolated microphone capture.

Windows uses the bundled EditPathAudio WASAPI helper. FFmpeg remains the
portable fallback for non-Windows integrations that have not adopted the Qt
helper yet.
"""
from __future__ import annotations

import os
import subprocess
import shutil
import sys
import time
from pathlib import Path
from typing import Callable

from .io import write_json


class AudioCapture:
    def __init__(self, session: Path, *, ffmpeg: str = "ffmpeg", segment_seconds: int = 30,
                 popen: Callable = subprocess.Popen) -> None:
        self.session, self.ffmpeg, self.segment_seconds, self._popen = session, ffmpeg, segment_seconds, popen
        configured_helper = os.environ.get("EDIT_PATH_AUDIO_HELPER", "")
        helper_candidates = [Path(configured_helper)] if configured_helper else []
        if os.name == "nt":
            helper_candidates.extend([
                Path(sys.executable).resolve().parent / "EditPathAudio.exe",
                Path(__file__).resolve().parents[1] / "EditPathAudio.exe",
            ])
            found = shutil.which("EditPathAudio.exe")
            if found:
                helper_candidates.append(Path(found))
        self.helper = next((path for path in helper_candidates if path.is_file()), None)
        self.process: subprocess.Popen | None = None
        self.index = 0

    def command(self, output: Path) -> list[str]:
        if self.helper is not None:
            device = os.environ.get("EDIT_PATH_MICROPHONE_DEVICE", "default")
            return [str(self.helper), "--device", device, "--output", str(output)]
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
        # A resumed GUI process starts with a fresh counter.  Never reuse an
        # existing filename: doing so would silently destroy an earlier
        # think-aloud segment from the same session.
        reasoning_dir = self.session / "EDIT-PATH" / "reasoning"
        reasoning_dir.mkdir(parents=True, exist_ok=True)
        existing = {
            int(path.stem.removeprefix("audio-"))
            for pattern in ("audio-*.wav", "audio-*.flac")
            for path in reasoning_dir.glob(pattern)
            if path.stem.removeprefix("audio-").isdigit()
        }
        self.index = max(self.index, max(existing, default=0)) + 1
        suffix = ".wav" if self.helper is not None else ".flac"
        output = reasoning_dir / f"audio-{self.index:03d}{suffix}"
        output.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.process = self._popen(self.command(output), stdin=subprocess.PIPE if self.helper is not None else subprocess.DEVNULL,
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
            if self.helper is not None and process.stdin is not None:
                process.stdin.write(b"q\n")
                process.stdin.close()
            else:
                process.terminate()
            process.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            try:
                process.kill()
            except OSError:
                pass
