# SPDX-FileCopyrightText: 2026 Edit Path contributors
# SPDX-License-Identifier: GPL-3.0-only

from __future__ import annotations

import json
import re
import shutil
import subprocess
from fractions import Fraction
from pathlib import Path

from .errors import EditPathError
from .io import sha256_file, write_json


SSIM_PATTERN = re.compile(r"All:([0-9.]+)")


def probe(path: Path, ffprobe_binary: str | None = None) -> dict:
    ffprobe = ffprobe_binary or shutil.which("ffprobe")
    if not ffprobe:
        raise EditPathError("ffprobe is required for media validation")
    completed = subprocess.run(
        [ffprobe, "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
        text=True,
        capture_output=True,
    )
    if completed.returncode != 0:
        raise EditPathError(f"ffprobe failed for {path}: {completed.stderr[-2000:]}")
    return json.loads(completed.stdout)


def validate_render(
    reference: Path,
    reconstructed: Path,
    report_path: Path | None = None,
    minimum_ssim: float = 0.995,
    maximum_duration_delta: float = 0.05,
    ffmpeg_binary: str | None = None,
    ffprobe_binary: str | None = None,
) -> dict:
    ffmpeg = ffmpeg_binary or shutil.which("ffmpeg")
    if not ffmpeg:
        raise EditPathError("ffmpeg is required for media validation")
    reference = reference.resolve()
    reconstructed = reconstructed.resolve()
    reference_probe = probe(reference, ffprobe_binary)
    reconstructed_probe = probe(reconstructed, ffprobe_binary)
    reference_duration = float(reference_probe.get("format", {}).get("duration", 0.0))
    reconstructed_duration = float(reconstructed_probe.get("format", {}).get("duration", 0.0))
    duration_delta = abs(reference_duration - reconstructed_duration)
    reference_video = [stream for stream in reference_probe.get("streams", []) if stream.get("codec_type") == "video"]
    reconstructed_video = [stream for stream in reconstructed_probe.get("streams", []) if stream.get("codec_type") == "video"]
    video_structure_match = len(reference_video) == len(reconstructed_video)
    if video_structure_match:
        for expected, actual in zip(reference_video, reconstructed_video):
            video_structure_match = video_structure_match and expected.get("width") == actual.get("width")
            video_structure_match = video_structure_match and expected.get("height") == actual.get("height")
            try:
                video_structure_match = video_structure_match and Fraction(expected.get("avg_frame_rate", "0/1")) == Fraction(actual.get("avg_frame_rate", "0/1"))
            except (ValueError, ZeroDivisionError):
                video_structure_match = False
    if reference_video and reconstructed_video:
        completed = subprocess.run(
            [
                ffmpeg,
                "-v",
                "info",
                "-i",
                str(reference),
                "-i",
                str(reconstructed),
                "-lavfi",
                "[0:v:0]settb=AVTB,setpts=PTS-STARTPTS[a];[1:v:0]settb=AVTB,setpts=PTS-STARTPTS[b];[a][b]ssim",
                "-f",
                "null",
                "-",
            ],
            text=True,
            capture_output=True,
        )
        matches = SSIM_PATTERN.findall(completed.stderr)
        if completed.returncode != 0 or not matches:
            raise EditPathError(f"ffmpeg SSIM comparison failed: {completed.stderr[-3000:]}")
        ssim = float(matches[-1])
        ssim_status = "measured"
    elif not reference_video and not reconstructed_video:
        # SSIM is undefined for audio-only/empty checkpoints. Matching stream
        # and duration structure is still validated; do not invoke an invalid
        # FFmpeg video filter graph.
        ssim = 1.0
        ssim_status = "not_applicable_no_video"
    else:
        ssim = 0.0
        ssim_status = "failed_missing_video_stream"
    reference_audio = [stream for stream in reference_probe.get("streams", []) if stream.get("codec_type") == "audio"]
    reconstructed_audio = [stream for stream in reconstructed_probe.get("streams", []) if stream.get("codec_type") == "audio"]
    audio_structure_match = len(reference_audio) == len(reconstructed_audio)
    if audio_structure_match:
        for expected, actual in zip(reference_audio, reconstructed_audio):
            audio_structure_match = audio_structure_match and expected.get("channels") == actual.get("channels")
            audio_structure_match = audio_structure_match and expected.get("sample_rate") == actual.get("sample_rate")
    accepted = ssim >= minimum_ssim and duration_delta <= maximum_duration_delta and audio_structure_match and video_structure_match
    report = {
        "schema": "edit-path/validation@2",
        "accepted": accepted,
        "minimum_ssim": minimum_ssim,
        "maximum_duration_delta_seconds": maximum_duration_delta,
        "ssim": ssim,
        "ssim_status": ssim_status,
        "duration_delta_seconds": duration_delta,
        "video_structure_match": video_structure_match,
        "audio_structure_match": audio_structure_match,
        "reference": {"sha256": sha256_file(reference), "probe": reference_probe},
        "reconstructed": {"sha256": sha256_file(reconstructed), "probe": reconstructed_probe},
    }
    if report_path is not None:
        write_json(report_path, report)
    return report
