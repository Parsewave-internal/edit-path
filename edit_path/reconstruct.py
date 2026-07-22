# SPDX-FileCopyrightText: 2026 Edit Path contributors
# SPDX-License-Identifier: GPL-3.0-only

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

from .assets import load_manifest, remap_project_assets, verify_assets
from .errors import EditPathError
from .io import find_trajectory, read_jsonl
from .state import load_state_reference


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
) -> Path:
    executable = melt_binary or shutil.which("melt") or shutil.which("mlt-melt")
    if not executable:
        raise EditPathError("melt/MLT is not installed or not on PATH")
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f".{output.stem}.tmp-{os.getpid()}{output.suffix}")
    settings = {
        "f": "mp4",
        "vcodec": "libx264",
        "crf": "18",
        "preset": "medium",
        "acodec": "aac",
        "ab": "192k",
        "movflags": "+faststart",
    }
    if preset:
        settings.update({str(key): str(value) for key, value in preset.items()})
    command = [executable, "-progress", str(project), "-consumer", f"avformat:{temporary}"]
    command.extend(f"{key}={value}" for key, value in settings.items())
    completed = subprocess.run(command, cwd=project.parent, text=True, capture_output=True)
    if completed.returncode != 0 or not temporary.is_file():
        temporary.unlink(missing_ok=True)
        detail = (completed.stderr or completed.stdout)[-4000:]
        raise EditPathError(f"melt reconstruction failed ({completed.returncode}):\n{detail}")
    os.replace(temporary, output)
    return output


def render_event(
    session_dir: Path,
    event: dict[str, Any],
    output: Path,
    *,
    melt_binary: str | None = None,
    preset: dict[str, str] | None = None,
) -> Path:
    project = output.with_suffix(".kdenlive")
    materialize_event_project(session_dir, event, project)
    return render_project(project, output, melt_binary=melt_binary, preset=preset)


def render_session(
    session_dir: Path,
    output: Path | None = None,
    *,
    melt_binary: str | None = None,
    preset: dict[str, str] | None = None,
) -> Path:
    session_dir = session_dir.expanduser().resolve()
    selected_output = output or session_dir / "reconstructed.mp4"
    project = materialize_project(session_dir, selected_output.with_suffix(".kdenlive"))
    return render_project(
        project,
        selected_output,
        melt_binary=melt_binary,
        preset=preset,
    )
