# SPDX-FileCopyrightText: 2026 Edit Path contributors
# SPDX-License-Identifier: GPL-3.0-only

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from .assets import load_manifest, remap_project_assets, verify_assets
from .errors import EditPathError
from .io import find_trajectory, read_jsonl
from .state import load_state_reference


VIDEO_ENCODER_PREFERENCE = ("libx264", "libopenh264", "mpeg4")


def available_video_encoders(ffmpeg_binary: str | None = None) -> set[str]:
    """Return the video encoders exposed by the colocated FFmpeg runtime."""

    executable = ffmpeg_binary or shutil.which("ffmpeg")
    if not executable:
        raise EditPathError("ffmpeg is required to select a reconstruction video encoder")
    completed = subprocess.run([executable, "-hide_banner", "-encoders"], text=True, capture_output=True)
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout)[-2000:]
        raise EditPathError(f"ffmpeg encoder discovery failed ({completed.returncode}):\n{detail}")
    encoders: set[str] = set()
    for line in completed.stdout.splitlines():
        match = re.match(r"^\s*V\S{5}\s+(\S+)", line)
        if match:
            encoders.add(match.group(1))
    return encoders


def select_video_encoder(ffmpeg_binary: str | None = None) -> str:
    """Select a deterministic MP4 encoder without assuming GPL libx264 exists."""

    available = available_video_encoders(ffmpeg_binary)
    forced = os.environ.get("EDIT_PATH_VIDEO_ENCODER")
    if forced:
        if forced not in available:
            raise EditPathError(f"requested video encoder {forced!r} is not available")
        return forced
    for encoder in VIDEO_ENCODER_PREFERENCE:
        if encoder in available:
            return encoder
    raise EditPathError(
        "no supported reconstruction video encoder is available; "
        f"expected one of {', '.join(VIDEO_ENCODER_PREFERENCE)}"
    )


def _encoder_settings(encoder: str) -> dict[str, str]:
    if encoder == "libx264":
        return {"vcodec": encoder, "crf": "18", "preset": "medium"}
    if encoder == "libopenh264":
        return {"vcodec": encoder, "vb": "8M", "g": "50"}
    if encoder == "mpeg4":
        return {"vcodec": encoder, "qscale": "2", "g": "50"}
    return {"vcodec": encoder}


def state_reference(event: dict[str, Any]) -> dict[str, Any] | None:
    value = event.get("project_state") or event.get("state_ref")
    if isinstance(value, dict):
        return value
    value = event.get("state")
    return value if isinstance(value, dict) else None


def latest_project_state(events: list[dict[str, Any]]) -> tuple[dict[str, Any], dict[str, Any]]:
    selected: tuple[dict[str, Any], dict[str, Any]] | None = None
    for event in events:
        reference = state_reference(event)
        if reference:
            selected = event, reference
    if selected is None:
        raise EditPathError("trajectory contains no exact reconstructable project state")
    return selected


def _reference_base(trajectory: Path, session_dir: Path, reference: dict[str, Any]) -> Path:
    base = reference.get("base")
    if base == "session":
        return session_dir
    return trajectory.parent


def materialize_event_project(
    session_dir: Path,
    event: dict[str, Any],
    output: Path,
    *,
    trajectory: Path | None = None,
    manifest: dict | None = None,
) -> Path:
    session_dir = session_dir.expanduser().resolve()
    trajectory = trajectory or find_trajectory(session_dir)
    reference = state_reference(event)
    if reference is None:
        raise EditPathError("selected event has no exact project state")
    raw = load_state_reference(reference, _reference_base(trajectory, session_dir, reference))
    if manifest is None:
        _, manifest = load_manifest(session_dir)
    verify_assets(session_dir, manifest)
    output.parent.mkdir(parents=True, exist_ok=True)
    project_xml = remap_project_assets(raw, output, manifest, session_dir)
    temporary = output.with_name(f".{output.name}.tmp-{os.getpid()}")
    temporary.write_bytes(project_xml)
    os.replace(temporary, output)
    return output


def materialize_project(session_dir: Path, output: Path | None = None) -> Path:
    session_dir = session_dir.expanduser().resolve()
    trajectory = find_trajectory(session_dir)
    events = read_jsonl(trajectory)
    try:
        event, _ = latest_project_state(events)
    except EditPathError:
        legacy_project = session_dir / "internal" / "final.kdenlive"
        if not legacy_project.is_file():
            raise
        _, manifest = load_manifest(session_dir)
        verify_assets(session_dir, manifest)
        destination = output or session_dir / "reconstructed.kdenlive"
        destination.parent.mkdir(parents=True, exist_ok=True)
        remapped = remap_project_assets(legacy_project.read_bytes(), destination, manifest, session_dir)
        temporary = destination.with_name(f".{destination.name}.tmp-{os.getpid()}")
        temporary.write_bytes(remapped)
        os.replace(temporary, destination)
        return destination
    return materialize_event_project(
        session_dir,
        event,
        output or session_dir / "reconstructed.kdenlive",
        trajectory=trajectory,
    )


def render_project(
    project: Path,
    output: Path,
    *,
    melt_binary: str | None = None,
    preset: dict[str, str] | None = None,
    require_video: bool = True,
    ffmpeg_binary: str | None = None,
    ffprobe_binary: str | None = None,
) -> Path:
    executable = melt_binary or shutil.which("melt") or shutil.which("mlt-melt")
    if not executable:
        raise EditPathError("melt/MLT is not installed or not on PATH")
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f".{output.stem}.tmp-{os.getpid()}{output.suffix}")
    overrides = {str(key): str(value) for key, value in (preset or {}).items()}
    explicit_encoder = overrides.pop("vcodec", None) or os.environ.get("EDIT_PATH_VIDEO_ENCODER")
    if explicit_encoder:
        candidates = [explicit_encoder]
    else:
        available = available_video_encoders(ffmpeg_binary)
        candidates = [encoder for encoder in VIDEO_ENCODER_PREFERENCE if encoder in available]
    if not candidates:
        raise EditPathError(
            "no supported reconstruction video encoder is available; "
            f"expected one of {', '.join(VIDEO_ENCODER_PREFERENCE)}"
        )

    failures: list[str] = []
    for encoder in candidates:
        temporary.unlink(missing_ok=True)
        settings = {
            "f": "mp4",
            "acodec": "aac",
            "ab": "192k",
            "movflags": "+faststart",
            # Reconstruction is an offline render. Without this consumer
            # setting MLT can throttle or spend minutes scheduling even very
            # short exact-state previews.
            "real_time": "-1",
            **_encoder_settings(encoder),
        }
        selected_overrides = dict(overrides)
        if encoder != "libx264":
            selected_overrides.pop("crf", None)
            selected_overrides.pop("preset", None)
        settings.update(selected_overrides)
        command = [executable, "-progress", str(project), "-consumer", f"avformat:{temporary}"]
        command.extend(f"{key}={value}" for key, value in settings.items())
        completed = subprocess.run(command, cwd=project.parent, text=True, capture_output=True)
        detail = (completed.stderr or completed.stdout)[-3000:]
        if completed.returncode != 0 or not temporary.is_file() or temporary.stat().st_size == 0:
            failures.append(f"{encoder}: Melt exited {completed.returncode}: {detail}")
            continue
        try:
            from .validate import probe

            media = probe(temporary, ffprobe_binary)
        except (EditPathError, json.JSONDecodeError, OSError) as error:
            failures.append(f"{encoder}: rendered output could not be probed: {error}")
            continue
        streams = media.get("streams", [])
        if not streams:
            failures.append(f"{encoder}: rendered output contains no media streams")
            continue
        if require_video and not any(stream.get("codec_type") == "video" for stream in streams):
            failures.append(f"{encoder}: rendered output contains no video stream: {detail}")
            continue
        os.replace(temporary, output)
        return output

    temporary.unlink(missing_ok=True)
    raise EditPathError("melt reconstruction failed for every usable encoder:\n" + "\n".join(failures))


def render_event(
    session_dir: Path,
    event: dict[str, Any],
    output: Path,
    *,
    melt_binary: str | None = None,
    preset: dict[str, str] | None = None,
    require_video: bool = True,
) -> Path:
    project = output.with_suffix(".kdenlive")
    materialize_event_project(session_dir, event, project)
    return render_project(project, output, melt_binary=melt_binary, preset=preset, require_video=require_video)


def render_session(
    session_dir: Path,
    output: Path | None = None,
    *,
    melt_binary: str | None = None,
    preset: dict[str, str] | None = None,
    require_video: bool = True,
) -> Path:
    session_dir = session_dir.expanduser().resolve()
    selected_output = output or session_dir / "reconstructed.mp4"
    project = materialize_project(session_dir, selected_output.with_suffix(".kdenlive"))
    return render_project(
        project,
        selected_output,
        melt_binary=melt_binary,
        preset=preset,
        require_video=require_video,
    )
