# SPDX-FileCopyrightText: 2026 Edit Path contributors
# SPDX-License-Identifier: GPL-3.0-only

from __future__ import annotations

import copy
import math
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from .assets import load_manifest
from .errors import EditPathError, GateError
from .io import event_sequence, replace_with_retry, sha256_file
from .reconstruct import render_event, select_video_encoder, state_reference
from .state import operation_name, validate_state_transitions
from .validate import probe


PROCESS_WIDTH = 1920
PROCESS_HEIGHT = 1080
PROCESS_FPS = 30

REPLAY_EVENT_TYPES = {
    "session.start",
    "session.end",
    "session.recovered",
    "state.checkpoint",
    "state.diff",
    "ui.command",
    "ui.shortcut",
    "ui.gesture",
}


def _project_hash(event: dict[str, Any]) -> str | None:
    reference = state_reference(event)
    if isinstance(reference, dict) and isinstance(reference.get("sha256"), str):
        return reference["sha256"]
    value = event.get("project_after_hash") or event.get("state_hash") or event.get("after_hash")
    return value if isinstance(value, str) else None


def build_replay_steps(
    events: list[dict[str, Any]],
    accepted: list[dict[str, Any]],
    baseline_hash: str,
) -> list[dict[str, Any]]:
    """Return the exact initial state followed by each accepted edit state."""

    _, state_reports = validate_state_transitions(events)
    snapshots = {
        report["event"].get("event_id"): report["snapshot"]
        for report in state_reports
        if isinstance(report.get("event"), dict)
    }
    baseline = next(
        (
            event
            for event in events
            if event.get("event_type") == "state.checkpoint" and _project_hash(event) == baseline_hash
        ),
        None,
    )
    if baseline is None:
        raise GateError("edit_process", "accepted branch baseline has no trajectory checkpoint")
    selected = [baseline, *sorted(accepted, key=lambda value: event_sequence(value) or -1)]
    steps: list[dict[str, Any]] = []
    for index, event in enumerate(selected):
        snapshot = snapshots.get(event.get("event_id"))
        if not isinstance(snapshot, dict):
            raise GateError("edit_process", "trajectory state has no reconstructed semantic snapshot", event_sequence(event))
        steps.append(
            {
                "index": index,
                "event": event,
                "snapshot": snapshot,
                "sequence": event_sequence(event),
                "operation": "timeline.initial_state" if index == 0 else operation_name(event.get("diff", {})),
                "label": "Initial blank timeline" if index == 0 else str(event.get("label") or "Edit"),
                "project_hash": _project_hash(event),
                "exact_project_state": state_reference(event) is not None,
            }
        )
    return steps


def _shortcut_operation(event: dict[str, Any]) -> str:
    value = str(event.get("key_sequence") or "Shortcut")
    lookup = {
        "Ctrl+C": "edit.copy",
        "Ctrl+V": "edit.paste",
        "Ctrl+X": "edit.cut",
        "Ctrl+Z": "history.undo",
        "Ctrl+Shift+Z": "history.redo",
        "Delete": "edit.delete",
        "Space": "transport.play_pause",
    }
    return lookup.get(value, f"keyboard.{value.lower().replace('+', '_')}")


def _event_operation(event: dict[str, Any], linked_diff: dict[str, Any] | None) -> str:
    event_type = event.get("event_type")
    if event_type == "state.diff":
        return operation_name(event.get("diff", {}))
    if event_type == "ui.shortcut":
        return _shortcut_operation(event)
    if event_type == "ui.command":
        command_id = event.get("command_id")
        if isinstance(command_id, str) and command_id != "unmapped":
            return f"command.{command_id}"
        return f"command.{str(event.get('label') or 'unknown').lower().replace(' ', '_')}"
    if event_type == "ui.gesture":
        if linked_diff is not None:
            return operation_name(linked_diff.get("diff", {}))
        return f"pointer.{event.get('gesture', 'interaction')}"
    if event_type == "state.checkpoint":
        return "timeline.checkpoint"
    return str(event_type)


def _empty_snapshot(events: list[dict[str, Any]], state_reports: list[dict[str, Any]]) -> dict[str, Any]:
    first = next((report.get("snapshot") for report in state_reports if isinstance(report.get("snapshot"), dict)), None)
    if first is not None:
        value = copy.deepcopy(first)
        value["duration_frames"] = 0
        for key in ("clips", "compositions", "mixes"):
            value[key] = []
        return value
    context = next((event.get("context") for event in events if event.get("event_type") == "project.context"), {})
    return {
        "timeline_id": context.get("project_id", "timeline"),
        "duration_frames": 0,
        "tracks": [],
        "clips": [],
        "compositions": [],
        "mixes": [],
        "master_effects": [],
    }


def build_replay_moments(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Join raw UI evidence to the semantic before/after states it caused."""

    _, state_reports = validate_state_transitions(events)
    state_after = {
        report["event"].get("event_id"): report["snapshot"]
        for report in state_reports
        if isinstance(report.get("event"), dict)
    }
    diffs = [event for event in events if event.get("event_type") == "state.diff"]
    by_interaction = {
        event.get("interaction_id"): event
        for event in diffs
        if isinstance(event.get("interaction_id"), str)
    }
    ordered = sorted(events, key=lambda value: event_sequence(value) or -1)
    empty = _empty_snapshot(events, state_reports)
    current_snapshot = copy.deepcopy(empty)
    current_state_event: dict[str, Any] | None = None
    moments: list[dict[str, Any]] = []
    for event in ordered:
        before_snapshot = copy.deepcopy(current_snapshot)
        after = state_after.get(event.get("event_id"))
        if isinstance(after, dict):
            current_snapshot = copy.deepcopy(after)
            current_state_event = event
        if event.get("event_type") not in REPLAY_EVENT_TYPES:
            continue
        linked_diff = by_interaction.get(event.get("interaction_id"))
        if linked_diff is None and event.get("event_type") in {"ui.gesture", "ui.shortcut"}:
            sequence = event_sequence(event) or -1
            linked_diff = next(
                (
                    candidate
                    for candidate in diffs
                    if sequence < (event_sequence(candidate) or -1) <= sequence + 4
                ),
                None,
            )
        moments.append(
            {
                "index": len(moments),
                "event": event,
                "sequence": event_sequence(event),
                "event_type": event.get("event_type"),
                "operation": _event_operation(event, linked_diff),
                "before_snapshot": before_snapshot,
                "snapshot": copy.deepcopy(current_snapshot),
                "state_event": current_state_event,
                "linked_diff": linked_diff,
            }
        )
    return moments


def _clean_text(value: object, *, maximum: int = 110) -> str:
    text = re.sub(r"[^A-Za-z0-9 ._()/+\-]", " ", str(value))
    return re.sub(r"\s+", " ", text).strip()[:maximum]


def _font_option() -> str:
    candidates = (
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
        Path("/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf"),
        Path("/System/Library/Fonts/Supplemental/Arial.ttf"),
        Path("/System/Library/Fonts/Helvetica.ttc"),
    )
    fc_match = shutil.which("fc-match")
    if fc_match:
        try:
            resolved = subprocess.run([fc_match, "-f", "%{file}", "sans-serif"], text=True,
                                      capture_output=True, check=False, timeout=3).stdout.strip()
            if resolved and Path(resolved).is_file():
                candidates = (Path(resolved),) + candidates
        except (OSError, subprocess.SubprocessError):
            pass
    font = next((path for path in candidates if path.is_file()), None)
    return f"fontfile={font}:" if font else "font=Sans:"


def _drawtext(
    text: object,
    *,
    x: int | str,
    y: int | str,
    size: int,
    color: str = "0xe5e7eb",
    box: bool = False,
    boxcolor: str = "black@0.70",
    borderw: int = 0,
) -> str:
    options = (
        f"drawtext={_font_option()}text='{_clean_text(text)}':x={x}:y={y}:"
        f"fontsize={size}:fontcolor={color}"
    )
    if box:
        options += f":box=1:boxcolor={boxcolor}:boxborderw=12"
    if borderw:
        options += f":borderw={borderw}:bordercolor=black@0.9"
    return options


def _clip_color(asset_id: object) -> str:
    palette = ("0x2563eb", "0x7c3aed", "0x0891b2", "0x16a34a", "0xea580c", "0xdb2777")
    return palette[sum(str(asset_id).encode("utf-8")) % len(palette)]


def _encoder_arguments(encoder: str) -> list[str]:
    if encoder == "libx264":
        return ["-c:v", encoder, "-preset", "veryfast", "-crf", "20"]
    if encoder == "libopenh264":
        return ["-c:v", encoder, "-b:v", "8M", "-g", "60"]
    if encoder == "mpeg4":
        return ["-c:v", encoder, "-q:v", "2", "-g", "60"]
    return ["-c:v", encoder]


def _run(command: list[str], description: str, *, cwd: Path | None = None) -> None:
    completed = subprocess.run(command, text=True, capture_output=True, cwd=cwd)
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout)[-5000:]
        raise EditPathError(f"{description} failed ({completed.returncode}):\n{detail}")


def _supports_filter(ffmpeg: str, name: str) -> bool:
    completed = subprocess.run(
        [ffmpeg, "-hide_banner", "-filters"],
        text=True,
        capture_output=True,
    )
    if completed.returncode != 0:
        return False
    listing = f"{completed.stdout}\n{completed.stderr}"
    return re.search(rf"^\s*[TSC\.]+\s+{re.escape(name)}\s", listing, re.MULTILINE) is not None


def _compatible_filters(filters: list[str], *, text_enabled: bool) -> list[str]:
    if text_enabled:
        return filters
    return [value for value in filters if not value.startswith("drawtext=")]


_DRAWTEXT_PATTERN = re.compile(
    r"^drawtext=(?:fontfile=[^:]+|font=[^:]+):text='(?P<text>[^']*)':"
    r"x=(?P<x>.*?):y=(?P<y>.*?):fontsize=(?P<size>\d+):fontcolor=(?P<color>[^:]+)(?P<options>.*)$"
)


def _parse_drawtext_filter(value: str) -> dict[str, Any]:
    match = _DRAWTEXT_PATTERN.match(value)
    if match is None:
        raise EditPathError(f"could not convert training label to the bundled text renderer: {value}")
    options = match.group("options")
    boxcolor = re.search(r":boxcolor=([^:]+)", options)
    borderw = re.search(r":borderw=(\d+)", options)
    return {
        "text": match.group("text"),
        "x": match.group("x"),
        "y": match.group("y"),
        "size": int(match.group("size")),
        "color": match.group("color"),
        "box": ":box=1" in options,
        "boxcolor": boxcolor.group(1) if boxcolor else "black@0.70",
        "borderw": int(borderw.group(1)) if borderw else 0,
    }


def _partition_text_filters(filters: list[str]) -> tuple[list[str], list[dict[str, Any]]]:
    video_filters: list[str] = []
    labels: list[dict[str, Any]] = []
    for value in filters:
        if value.startswith("drawtext="):
            labels.append(_parse_drawtext_filter(value))
        else:
            video_filters.append(value)
    return video_filters, labels


def _ass_color(value: str) -> str:
    named = {
        "black": "000000",
        "white": "FFFFFF",
        "red": "FF0000",
        "green": "00FF00",
        "blue": "0000FF",
    }
    color_value, _, opacity_value = value.partition("@")
    rgb = named.get(color_value.lower(), color_value.removeprefix("0x").removeprefix("#"))
    if not re.fullmatch(r"[0-9A-Fa-f]{6}", rgb):
        rgb = "FFFFFF"
    opacity = max(0.0, min(1.0, float(opacity_value))) if opacity_value else 1.0
    alpha = round((1.0 - opacity) * 255)
    red, green, blue = rgb[0:2], rgb[2:4], rgb[4:6]
    return f"&H{alpha:02X}{blue}{green}{red}&".upper()


_MOTION_EXPRESSION = re.compile(r"^(-?\d+)\+\((-?\d+)\)\*min\(t/[0-9.]+\\,1\)$")


def _ass_position(label: dict[str, Any], duration: float) -> str:
    x_value, y_value = str(label["x"]), str(label["y"])
    x_motion = _MOTION_EXPRESSION.match(x_value)
    y_motion = _MOTION_EXPRESSION.match(y_value)
    if x_motion and y_motion:
        start_x, delta_x = int(x_motion.group(1)), int(x_motion.group(2))
        start_y, delta_y = int(y_motion.group(1)), int(y_motion.group(2))
        end_ms = max(1, round(max(duration - 0.15, 0.1) * 1000))
        return f"\\move({start_x},{start_y},{start_x + delta_x},{start_y + delta_y},0,{end_ms})"
    try:
        x, y = round(float(x_value)), round(float(y_value))
    except ValueError as error:
        raise EditPathError(f"unsupported training label position: x={x_value}, y={y_value}") from error
    return f"\\pos({x},{y})"


def _ass_timestamp(seconds: float) -> str:
    centiseconds = max(0, round(seconds * 100))
    hours, remainder = divmod(centiseconds, 360000)
    minutes, remainder = divmod(remainder, 6000)
    whole_seconds, fraction = divmod(remainder, 100)
    return f"{hours}:{minutes:02d}:{whole_seconds:02d}.{fraction:02d}"


def _write_ass_labels(path: Path, labels: list[dict[str, Any]], duration: float) -> None:
    styles = []
    dialogues = []
    for index, label in enumerate(labels, 1):
        style = f"Label{index:03d}"
        primary = _ass_color(str(label["color"]))
        outline = _ass_color(str(label["boxcolor"])) if label["box"] else "&H00000000"
        border_style = 3 if label["box"] else 1
        outline_width = 8 if label["box"] else int(label["borderw"])
        styles.append(
            f"Style: {style},Arial,{label['size']},{primary},{primary},{outline},{outline},"
            f"0,0,0,0,100,100,0,0,{border_style},{outline_width},0,7,0,0,0,1"
        )
        position = _ass_position(label, duration)
        text = str(label["text"]).replace("\\", r"\\").replace("{", "(").replace("}", ")")
        dialogues.append(
            f"Dialogue: 0,{_ass_timestamp(0)},{_ass_timestamp(duration)},{style},,0,0,0,,"
            f"{{\\an7{position}}}{text}"
        )
    content = "\n".join(
        [
            "[Script Info]",
            "ScriptType: v4.00+",
            f"PlayResX: {PROCESS_WIDTH}",
            f"PlayResY: {PROCESS_HEIGHT}",
            "ScaledBorderAndShadow: yes",
            "WrapStyle: 2",
            "",
            "[V4+ Styles]",
            "Format: Name,Fontname,Fontsize,PrimaryColour,SecondaryColour,OutlineColour,BackColour,"
            "Bold,Italic,Underline,StrikeOut,ScaleX,ScaleY,Spacing,Angle,BorderStyle,Outline,Shadow,"
            "Alignment,MarginL,MarginR,MarginV,Encoding",
            *styles,
            "",
            "[Events]",
            "Format: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text",
            *dialogues,
            "",
        ]
    )
    path.write_text(content, encoding="utf-8")


def _scene_duration(moment: dict[str, Any]) -> float:
    event_type = moment["event_type"]
    if event_type in {"session.start", "session.end"}:
        return 2.0
    if event_type == "state.diff":
        return 1.8
    if event_type == "ui.gesture" and moment["event"].get("gesture") == "drag":
        return 1.5
    if event_type in {"ui.command", "ui.shortcut", "state.checkpoint"}:
        return 1.35
    return 1.1


def _asset_lookup(session_dir: Path) -> tuple[dict[str, str], list[Path]]:
    _, manifest = load_manifest(session_dir)
    names: dict[str, str] = {}
    paths: list[Path] = []
    for entry in manifest.get("assets", []):
        name = str(entry.get("original_filename") or Path(str(entry.get("file", "asset"))).name)
        for reference in entry.get("bin_references", []):
            names[str(reference)] = name
        if entry.get("bin_reference") is not None:
            names[str(entry["bin_reference"])] = name
        if entry.get("asset_id") is not None:
            names[str(entry["asset_id"])] = name
        relative = entry.get("file") or entry.get("path")
        if isinstance(relative, str):
            path = session_dir / relative
            if path.is_file():
                try:
                    media = probe(path)
                except EditPathError:
                    continue
                if any(stream.get("codec_type") == "video" for stream in media.get("streams", [])):
                    paths.append(path)
    return names, paths


def _selected_ids(moment: dict[str, Any]) -> set[str]:
    source = moment.get("linked_diff") or (moment["event"] if moment["event_type"] == "state.diff" else None)
    selected: set[str] = set()
    if isinstance(source, dict):
        for change in source.get("diff", {}).get("changes", []):
            if change.get("entity") == "clip":
                selected.add(str(change.get("native_id")))
    if not selected and moment["event_type"] == "ui.shortcut":
        clips = moment["snapshot"].get("clips", [])
        if clips:
            selected.add(str(clips[0].get("native_id")))
    return selected


def _track_rows(snapshot: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, int]]:
    tracks = sorted(snapshot.get("tracks", []), key=lambda value: int(value.get("position", 0)), reverse=True)
    tracks = tracks[:5]
    rows = {str(track.get("native_id")): index for index, track in enumerate(tracks)}
    return tracks, rows


def _playhead_frame(moment: dict[str, Any], maximum_frames: int) -> int:
    event = moment["event"]
    if event.get("event_type") == "ui.gesture":
        point = event.get("end_global") or event.get("start_global") or {}
        if isinstance(point.get("x"), int):
            return max(0, min(maximum_frames, round((point["x"] - 90) / 1100 * maximum_frames)))
    source = moment.get("linked_diff") or (event if event.get("event_type") == "state.diff" else None)
    if isinstance(source, dict):
        for change in source.get("diff", {}).get("changes", []):
            value = change.get("after") or change.get("before") or {}
            if isinstance(value.get("timeline_start_frame"), int):
                return max(0, min(maximum_frames, value["timeline_start_frame"]))
    clips = moment["snapshot"].get("clips", [])
    return int(clips[0].get("timeline_start_frame", 0)) if clips else 0


def _base_ui_filters(moment: dict[str, Any], *, moment_count: int, fps: float) -> list[str]:
    event = moment["event"]
    sequence = moment.get("sequence")
    timestamp = str(event.get("timestamp_utc") or "")
    timecode = timestamp[11:19] if len(timestamp) >= 19 else "--:--:--"
    operation = moment["operation"]
    filters = [
        "drawbox=x=0:y=0:w=iw:h=34:color=0x1b1e23:t=fill",
        "drawbox=x=0:y=34:w=iw:h=48:color=0x292d34:t=fill",
        "drawbox=x=0:y=82:w=400:h=520:color=0x20242a:t=fill",
        "drawbox=x=400:y=82:w=1060:h=520:color=0x181b20:t=fill",
        "drawbox=x=1460:y=82:w=460:h=520:color=0x20242a:t=fill",
        "drawbox=x=0:y=602:w=iw:h=438:color=0x171a1f:t=fill",
        "drawbox=x=0:y=1040:w=iw:h=40:color=0x24282e:t=fill",
        "drawbox=x=0:y=82:w=400:h=520:color=0x4b5563:t=1",
        "drawbox=x=400:y=82:w=1060:h=520:color=0x4b5563:t=1",
        "drawbox=x=1460:y=82:w=460:h=520:color=0x4b5563:t=1",
        "drawbox=x=430:y=118:w=1000:h=450:color=black:t=fill",
        _drawtext("EDIT PATH  /  Kdenlive training replay", x=18, y=7, size=17, color="0xf3f4f6"),
        _drawtext("File   Edit   Project   Tool   Clip   Timeline   Monitor   View   Help", x=430, y=8, size=15, color="0xd1d5db"),
        _drawtext(f"EVENT {sequence}   {str(moment['event_type']).upper()}   {timecode} UTC", x=1510, y=8, size=14, color="0x93c5fd"),
        _drawtext("Select", x=20, y=49, size=14, color="0x60a5fa" if operation.startswith("clip.move") or operation.startswith("clip.insert") else "0xe5e7eb"),
        _drawtext("Razor", x=92, y=49, size=14, color="0xfbbf24" if any(value in operation for value in ("cut", "split", "trim")) else "0xe5e7eb"),
        _drawtext("Spacer", x=160, y=49, size=14),
        _drawtext("Ripple", x=242, y=49, size=14),
        _drawtext("Undo", x=338, y=49, size=14, color="0xfbbf24" if operation == "history.undo" else "0xe5e7eb"),
        _drawtext("Redo", x=400, y=49, size=14, color="0xfbbf24" if operation == "history.redo" else "0xe5e7eb"),
        _drawtext("Render", x=492, y=49, size=14, color="0x93c5fd" if "render" in operation else "0xe5e7eb"),
        _drawtext("Project Bin", x=18, y=94, size=18, color="0xf3f4f6"),
        _drawtext("Search clips...", x=205, y=96, size=13, color="0x9ca3af", box=True, boxcolor="0x111318@0.9"),
        _drawtext("Project Monitor", x=425, y=94, size=17, color="0xf3f4f6"),
        _drawtext("Effects / Properties", x=1480, y=94, size=18, color="0xf3f4f6"),
        _drawtext("Effects", x=1490, y=130, size=15, color="0x60a5fa"),
        _drawtext("Properties", x=1580, y=130, size=15),
        "drawbox=x=1480:y=158:w=420:h=1:color=0x4b5563:t=fill",
        _drawtext("Transform", x=1490, y=178, size=15),
        _drawtext("Opacity", x=1490, y=215, size=15),
        _drawtext("Position and Zoom", x=1490, y=252, size=15),
        _drawtext("Audio correction", x=1490, y=289, size=15),
        _drawtext("Timeline", x=18, y=614, size=18, color="0xf3f4f6"),
        _drawtext("00:00:00:00", x=1515, y=614, size=16, color="0x93c5fd"),
        _drawtext("|<      <<       PLAY       >>      >|", x=805, y=575, size=17, color="0xd1d5db"),
        _drawtext(f"Replay event {moment['index'] + 1}/{moment_count}", x=18, y=1052, size=15, color="0x9ca3af"),
        _drawtext(f"{operation}  /  exact semantic state at {fps:.2f} fps", x=590, y=1052, size=15, color="0xd1d5db"),
        _drawtext(f"seq {sequence}", x=1830, y=1052, size=14, color="0x9ca3af"),
    ]
    return filters


def _timeline_filters(
    moment: dict[str, Any],
    *,
    maximum_frames: int,
    fps: float,
    asset_names: dict[str, str],
) -> list[str]:
    snapshot = moment["snapshot"]
    tracks, rows = _track_rows(snapshot)
    selected = _selected_ids(moment)
    timeline_x = 140
    timeline_width = 1740
    row_start = 700
    row_height = 64
    denominator = max(1, maximum_frames)
    filters: list[str] = [
        "drawbox=x=0:y=650:w=iw:h=40:color=0x22262c:t=fill",
        "drawbox=x=140:y=650:w=1740:h=40:color=0x2b3037:t=fill",
    ]
    total_seconds = denominator / max(fps, 0.001)
    tick_seconds = 5 if total_seconds > 15 else 2
    tick = 0
    while tick <= total_seconds + 0.001:
        x = timeline_x + round(tick / max(total_seconds, 0.001) * timeline_width)
        filters.append(f"drawbox=x={x}:y=650:w=1:h=390:color=0x4b5563@0.65:t=fill")
        filters.append(_drawtext(f"00:{int(tick) // 60:02d}:{int(tick) % 60:02d}", x=x + 4, y=660, size=12, color="0x9ca3af"))
        tick += tick_seconds
    for row, track in enumerate(tracks):
        y = row_start + row * row_height
        shade = "0x20242a" if row % 2 == 0 else "0x1b1f24"
        filters.extend(
            [
                f"drawbox=x=0:y={y}:w=iw:h={row_height - 2}:color={shade}:t=fill",
                f"drawbox=x={timeline_x}:y={y}:w={timeline_width}:h={row_height - 2}:color=0x4b5563:t=1",
                _drawtext(str(track.get("tag") or f"Track {row + 1}"), x=18, y=y + 9, size=17, color="0xf3f4f6"),
                _drawtext(
                    "VIDEO" if track.get("kind") == "video" else "AUDIO",
                    x=78,
                    y=y + 12,
                    size=11,
                    color="0x60a5fa" if track.get("kind") == "video" else "0x34d399",
                ),
                _drawtext("M  S  L", x=18, y=y + 37, size=11, color="0x9ca3af"),
            ]
        )
    for clip in snapshot.get("clips", []):
        row = rows.get(str(clip.get("track_native_id")))
        if row is None:
            continue
        start = max(0, int(clip.get("timeline_start_frame", 0) or 0))
        duration = max(1, int(clip.get("duration_frames", 1) or 1))
        x = timeline_x + round(start / denominator * timeline_width)
        width = max(12, round(duration / denominator * timeline_width))
        width = min(width, timeline_x + timeline_width - x)
        y = row_start + row * row_height + 5
        color = _clip_color(clip.get("asset_id") or clip.get("asset_reference"))
        filters.append(f"drawbox=x={x}:y={y}:w={width}:h={row_height - 12}:color={color}@0.82:t=fill")
        filters.append(f"drawbox=x={x}:y={y}:w={width}:h={row_height - 12}:color=white@0.95:t={3 if str(clip.get('native_id')) in selected else 1}")
        name = asset_names.get(str(clip.get("asset_reference")), f"Clip {clip.get('native_id')}")
        filters.append(_drawtext(name, x=x + 8, y=y + 7, size=13, color="white", borderw=1))
        filters.append(_drawtext(f"{duration / max(fps, 0.001):.2f}s", x=x + 8, y=y + 30, size=11, color="0xdbeafe"))
        if clip.get("effects"):
            filters.append(_drawtext("fx", x=x + max(8, width - 30), y=y + 7, size=12, color="0xfde68a"))
    playhead = _playhead_frame(moment, denominator)
    playhead_x = timeline_x + round(playhead / denominator * timeline_width)
    filters.extend(
        [
            f"drawbox=x={playhead_x}:y=645:w=2:h=395:color=0xef4444@0.95:t=fill",
            f"drawbox=x={playhead_x - 5}:y=645:w=12:h=10:color=0xef4444:t=fill",
        ]
    )
    return filters


def _properties_filters(moment: dict[str, Any], *, fps: float) -> list[str]:
    selected = _selected_ids(moment)
    clip = next(
        (value for value in moment["snapshot"].get("clips", []) if str(value.get("native_id")) in selected),
        None,
    )
    if clip is None:
        return [
            _drawtext("No clip selected", x=1490, y=350, size=16, color="0x9ca3af"),
            _drawtext("Select a clip to inspect effects", x=1490, y=382, size=13, color="0x6b7280"),
        ]
    effects = clip.get("effects", [])
    values = [
        f"Clip ID       {clip.get('native_id')}",
        f"Track         {clip.get('track_native_id')}",
        f"Start         {int(clip.get('timeline_start_frame', 0)) / max(fps, 0.001):.2f} s",
        f"Duration      {int(clip.get('duration_frames', 0)) / max(fps, 0.001):.2f} s",
        f"Speed         {clip.get('speed', 1)}x",
        f"Effects       {len(effects)}",
    ]
    filters = ["drawbox=x=1480:y=330:w=420:h=230:color=0x171a1f@0.9:t=fill"]
    for index, value in enumerate(values):
        filters.append(_drawtext(value, x=1495, y=345 + index * 33, size=14, color="0xd1d5db"))
    if effects:
        effect = effects[0] if isinstance(effects[0], dict) else {"name": effects[0]}
        name = effect.get("name") or effect.get("service") or effect.get("mlt_service") or effect.get("id") or "Effect"
        filters.append(_drawtext(f"Active effect  {name}", x=1495, y=548, size=14, color="0xfde68a"))
    return filters


def _semantic_pointer_coordinates(moment: dict[str, Any], maximum_frames: int) -> tuple[int, int, int, int] | None:
    linked = moment.get("linked_diff")
    if not isinstance(linked, dict):
        return None
    change = next(
        (value for value in linked.get("diff", {}).get("changes", []) if value.get("entity") == "clip"),
        None,
    )
    if not isinstance(change, dict):
        return None
    before = change.get("before") if isinstance(change.get("before"), dict) else None
    after = change.get("after") if isinstance(change.get("after"), dict) else None
    if before is None and after is None:
        return None
    tracks, rows = _track_rows(moment["snapshot"])
    if not tracks:
        tracks, rows = _track_rows(moment["before_snapshot"])

    def point(value: dict[str, Any] | None, fallback: dict[str, Any] | None) -> tuple[int, int]:
        selected = value or fallback or {}
        start = int(selected.get("timeline_start_frame", 0) or 0)
        duration = int(selected.get("duration_frames", 1) or 1)
        x = 140 + round((start + duration / 2) / max(1, maximum_frames) * 1740)
        row = rows.get(str(selected.get("track_native_id")), 0)
        y = 700 + row * 64 + 28
        return x, y

    sx, sy = point(before, after)
    ex, ey = point(after, before)
    return sx, sy, ex, ey


def _action_filters(moment: dict[str, Any], *, scene_duration: float, maximum_frames: int) -> list[str]:
    event = moment["event"]
    event_type = moment["event_type"]
    operation = moment["operation"]
    filters: list[str] = []
    if event_type == "ui.shortcut":
        shortcut = str(event.get("key_sequence") or "Shortcut")
        filters.extend(
            [
                "drawbox=x=720:y=430:w=480:h=120:color=0x0f172a@0.90:t=fill",
                "drawbox=x=720:y=430:w=480:h=120:color=0x60a5fa@0.95:t=2",
                _drawtext("KEYBOARD SHORTCUT", x=860, y=450, size=15, color="0x93c5fd"),
                _drawtext(shortcut.replace("+", "  +  "), x=805, y=486, size=31, color="white", box=True, boxcolor="0x374151@0.95"),
            ]
        )
    elif event_type == "ui.command":
        label = event.get("label") or event.get("command_id") or "Command"
        filters.extend(
            [
                "drawbox=x=1455:y=520:w=455:h=68:color=0x0f172a@0.92:t=fill",
                "drawbox=x=1455:y=520:w=455:h=68:color=0x60a5fa@0.9:t=2",
                _drawtext("COMMAND", x=1475, y=532, size=12, color="0x93c5fd"),
                _drawtext(label, x=1475, y=551, size=20, color="white"),
            ]
        )
        if "render" in operation:
            filters.extend(
                [
                    "drawbox=x=650:y=270:w=620:h=300:color=0x111827@0.96:t=fill",
                    "drawbox=x=650:y=270:w=620:h=300:color=0x60a5fa:t=2",
                    _drawtext("Render Project", x=680, y=292, size=25, color="white"),
                    _drawtext("Output file    final.mkv", x=690, y=350, size=17),
                    _drawtext("Preset         Lossless FFV1", x=690, y=390, size=17),
                    _drawtext("Full project   00:00:28:07", x=690, y=430, size=17),
                    "drawbox=x=690:y=487:w=540:h=18:color=0x374151:t=fill",
                    "drawbox=x=690:y=487:w=460:h=18:color=0x3b82f6:t=fill",
                    _drawtext("Rendering trajectory-verified output", x=690, y=520, size=15, color="0x93c5fd"),
                ]
            )
    elif event_type == "state.diff":
        change_count = len(event.get("diff", {}).get("changes", []))
        filters.extend(
            [
                "drawbox=x=730:y=88:w=460:h=64:color=0x052e16@0.94:t=fill",
                "drawbox=x=730:y=88:w=460:h=64:color=0x34d399:t=2",
                _drawtext("APPLIED", x=750, y=101, size=12, color="0x6ee7b7"),
                _drawtext(f"{operation}   {change_count} change(s)", x=750, y=120, size=20, color="white"),
            ]
        )
    elif event_type == "state.checkpoint":
        filters.extend(
            [
                "drawbox=x=735:y=88:w=450:h=56:color=0x172554@0.92:t=fill",
                _drawtext("EXACT TIMELINE CHECKPOINT LOADED", x=770, y=105, size=18, color="0x93c5fd"),
            ]
        )
    elif event_type == "session.recovered":
        filters.extend(
            [
                "drawbox=x=700:y=330:w=520:h=180:color=0x111827@0.96:t=fill",
                "drawbox=x=700:y=330:w=520:h=180:color=0xf59e0b:t=2",
                _drawtext("SESSION RECOVERED", x=820, y=360, size=24, color="0xfbbf24"),
                _drawtext(event.get("reason") or "Recovery segment opened", x=750, y=410, size=17),
                _drawtext("Continuing from durable trajectory evidence", x=750, y=452, size=15, color="0x9ca3af"),
            ]
        )
    elif event_type in {"session.start", "session.end"}:
        title = "EDITING SESSION START" if event_type == "session.start" else "EDITING SESSION COMPLETE"
        subtitle = "Raw commands and exact states are now recording" if event_type == "session.start" else "Trajectory closed with durable state sidecars"
        filters.extend(
            [
                "drawbox=x=620:y=350:w=680:h=180:color=0x0f172a@0.96:t=fill",
                "drawbox=x=620:y=350:w=680:h=180:color=0x3b82f6:t=2",
                _drawtext(title, x=725, y=385, size=29, color="white"),
                _drawtext(subtitle, x=690, y=445, size=17, color="0x93c5fd"),
                _drawtext("EDIT PATH  /  training reconstruction", x=800, y=485, size=14, color="0x9ca3af"),
            ]
        )
    if event_type == "ui.gesture":
        semantic = _semantic_pointer_coordinates(moment, maximum_frames)
        if semantic is None:
            start = event.get("start_global") or event.get("end_global") or {"x": 800, "y": 600}
            end = event.get("end_global") or start
            sx = max(0, min(PROCESS_WIDTH - 30, round(float(start.get("x", 800)) * PROCESS_WIDTH / 1280)))
            sy = max(0, min(PROCESS_HEIGHT - 30, round(float(start.get("y", 600)) * PROCESS_HEIGHT / 720)))
            ex = max(0, min(PROCESS_WIDTH - 30, round(float(end.get("x", sx)) * PROCESS_WIDTH / 1280)))
            ey = max(0, min(PROCESS_HEIGHT - 30, round(float(end.get("y", sy)) * PROCESS_HEIGHT / 720)))
        else:
            sx, sy, ex, ey = semantic
        progress = f"min(t/{max(scene_duration - 0.15, 0.1):.3f}\\,1)"
        x_expression = f"{sx}+({ex - sx})*{progress}"
        y_expression = f"{sy}+({ey - sy})*{progress}"
        filters.extend(
            [
                _drawtext("+", x=x_expression, y=y_expression, size=28, color="white", box=True, boxcolor="0x2563eb@0.85", borderw=1),
                _drawtext(str(event.get("gesture") or "pointer"), x=1515, y=575, size=14, color="0x93c5fd", box=True, boxcolor="0x111827@0.85"),
            ]
        )
    return filters


def _render_scene(
    ffmpeg: str,
    encoder: str,
    moment: dict[str, Any],
    destination: Path,
    *,
    preview: Path | None,
    preview_seek: float,
    asset: Path | None,
    asset_names: dict[str, str],
    maximum_frames: int,
    fps: float,
    moment_count: int,
    text_renderer: str,
) -> int:
    duration = _scene_duration(moment)
    command = [
        ffmpeg,
        "-y",
        "-v",
        "error",
        "-f",
        "lavfi",
        "-i",
        f"color=c=0x111318:s={PROCESS_WIDTH}x{PROCESS_HEIGHT}:r={PROCESS_FPS}:d={duration:.3f}",
    ]
    preview_index: int | None = None
    asset_index: int | None = None
    next_index = 1
    if preview is not None:
        command.extend(["-stream_loop", "-1", "-ss", f"{preview_seek:.3f}", "-i", str(preview)])
        preview_index = next_index
        next_index += 1
    if asset is not None:
        command.extend(["-stream_loop", "-1", "-i", str(asset)])
        asset_index = next_index

    base_filters = _base_ui_filters(moment, moment_count=moment_count, fps=fps)
    ass_labels: list[dict[str, Any]] = []
    if text_renderer in {"ass", "none"}:
        base_filters, ass_labels = _partition_text_filters(base_filters)
    graph = [f"[0:v]{','.join(base_filters)}[ui0]"]
    current = "ui0"
    label_index = 1
    if preview_index is not None:
        graph.append(
            f"[{preview_index}:v]fps={PROCESS_FPS},scale=900:450:force_original_aspect_ratio=decrease,"
            f"pad=900:450:(ow-iw)/2:(oh-ih)/2:color=black,trim=duration={duration:.3f},setpts=PTS-STARTPTS[monitor]"
        )
        graph.append(f"[{current}][monitor]overlay=480:118:eof_action=pass:shortest=0[ui{label_index}]")
        current = f"ui{label_index}"
        label_index += 1
    if asset_index is not None:
        graph.append(
            f"[{asset_index}:v]fps={PROCESS_FPS},scale=180:112:force_original_aspect_ratio=decrease,"
            f"pad=180:112:(ow-iw)/2:(oh-ih)/2:color=0x111318,trim=duration={duration:.3f},setpts=PTS-STARTPTS[thumb]"
        )
        graph.append(f"[{current}][thumb]overlay=20:135:eof_action=pass:shortest=0[ui{label_index}]")
        current = f"ui{label_index}"
        label_index += 1

    filters = [
        "drawbox=x=470:y=110:w=920:h=466:color=0x6b7280:t=1",
        "drawbox=x=16:y=130:w=188:h=120:color=0x4b5563:t=1",
    ]
    first_asset_name = next(iter(asset_names.values()), "Source media")
    filters.extend(
        [
            _drawtext(first_asset_name, x=20, y=260, size=14, color="0xe5e7eb"),
            _drawtext("Media clip", x=20, y=288, size=12, color="0x9ca3af"),
            *_timeline_filters(moment, maximum_frames=maximum_frames, fps=fps, asset_names=asset_names),
            *_properties_filters(moment, fps=fps),
            *_action_filters(moment, scene_duration=duration, maximum_frames=maximum_frames),
        ]
    )
    if text_renderer in {"ass", "none"}:
        filters, action_labels = _partition_text_filters(filters)
        ass_labels.extend(action_labels)
    if text_renderer == "ass":
        graph.append(f"[{current}]{','.join(filters)}[prelabels]")
        ass_path = destination.with_suffix(".ass")
        _write_ass_labels(ass_path, ass_labels, duration)
        graph.append(f"[prelabels]ass=filename='{ass_path.name}'[video]")
    else:
        graph.append(f"[{current}]{','.join(filters)}[video]")
    command.extend(
        [
            "-filter_complex",
            ";".join(graph),
            "-map",
            "[video]",
            "-an",
            "-r",
            str(PROCESS_FPS),
            "-pix_fmt",
            "yuv420p",
            *_encoder_arguments(encoder),
            "-t",
            f"{duration:.3f}",
            "-movflags",
            "+faststart",
            str(destination),
        ]
    )
    _run(command, f"editor replay event {moment.get('sequence')}", cwd=destination.parent)
    if text_renderer == "ass":
        return len(ass_labels)
    if text_renderer == "none":
        return 0
    return sum(
        value.startswith("drawtext=")
        for value in [*_base_ui_filters(moment, moment_count=moment_count, fps=fps), *filters]
    )


def render_edit_process(
    session_dir: Path,
    events: list[dict[str, Any]],
    accepted: list[dict[str, Any]],
    baseline_hash: str,
    output: Path,
    work_dir: Path,
    *,
    melt_binary: str | None = None,
) -> tuple[Path, dict[str, Any]]:
    """Render raw commands and exact states as a training-quality editor replay."""

    session_dir = session_dir.expanduser().resolve()
    output = output.expanduser().resolve()
    work_dir = work_dir.expanduser().resolve()
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise EditPathError("ffmpeg is required to render the editing-process video")
    encoder = select_video_encoder(ffmpeg)
    text_renderer = "drawtext" if _supports_filter(ffmpeg, "drawtext") else "ass" if _supports_filter(ffmpeg, "ass") else "none"
    build_replay_steps(events, accepted, baseline_hash)
    moments = build_replay_moments(events)
    context = next((event.get("context") for event in events if event.get("event_type") == "project.context"), {})
    numerator = float(context.get("fps_numerator", 25) or 25)
    denominator = float(context.get("fps_denominator", 1) or 1)
    fps = numerator / denominator
    maximum_frames = max(
        1,
        *(int(moment["snapshot"].get("duration_frames", 0) or 0) for moment in moments),
    )
    asset_names, asset_paths = _asset_lookup(session_dir)
    asset = asset_paths[0] if asset_paths else None

    work_dir.mkdir(parents=True, exist_ok=True)
    preview_dir = work_dir / "state-previews"
    scene_dir = work_dir / "event-scenes"
    preview_dir.mkdir(parents=True, exist_ok=True)
    scene_dir.mkdir(parents=True, exist_ok=True)
    preview_cache: dict[str, tuple[Path, float]] = {}
    scene_paths: list[Path] = []
    moment_reports: list[dict[str, Any]] = []
    text_overlay_count = 0
    text_warnings: list[str] = []
    preview_warnings: list[str] = []
    active_text_renderer = text_renderer

    for moment in moments:
        snapshot = moment["snapshot"]
        state_event = moment.get("state_event")
        preview: Path | None = None
        preview_seek = 0.0
        preview_mode = "semantic_ui_only"
        if (
            isinstance(state_event, dict)
            and state_reference(state_event) is not None
            and int(snapshot.get("duration_frames", 0) or 0) > 0
            and (snapshot.get("clips") or snapshot.get("compositions"))
        ):
            digest = _project_hash(state_event) or str(moment["sequence"])
            cached = preview_cache.get(digest)
            if cached is None:
                preview_path = preview_dir / f"{digest}.mp4"
                try:
                    render_event(
                        session_dir,
                        state_event,
                        preview_path,
                        melt_binary=melt_binary,
                        preset={"crf": "23", "preset": "veryfast", "ab": "96k"},
                    )
                    media = probe(preview_path)
                    preview_duration = float(media.get("format", {}).get("duration", 0.0))
                    if preview_duration <= 0 or not any(stream.get("codec_type") == "video" for stream in media.get("streams", [])):
                        raise EditPathError("exact state preview contains no usable video")
                    cached = preview_path, preview_duration
                    preview_cache[digest] = cached
                except (EditPathError, OSError, ValueError) as error:
                    preview_warnings.append(
                        f"exact state preview failed at sequence {moment['sequence']}; semantic editor state used instead: {error}"
                    )
            if cached is not None:
                preview, preview_duration = cached
                selected = _selected_ids(moment)
                selected_clip = next(
                    (clip for clip in snapshot.get("clips", []) if str(clip.get("native_id")) in selected),
                    None,
                )
                if selected_clip is None and snapshot.get("clips"):
                    selected_clip = snapshot["clips"][0]
                if selected_clip is not None:
                    preview_seek = min(
                        max(0.0, float(selected_clip.get("timeline_start_frame", 0)) / max(fps, 0.001) + 0.08),
                        max(0.0, preview_duration - 0.1),
                    )
                preview_mode = "exact_project_monitor"

        scene = scene_dir / f"event-{moment['index'] + 1:03d}.mp4"
        try:
            text_overlay_count += _render_scene(
                ffmpeg,
                encoder,
                moment,
                scene,
                preview=preview,
                preview_seek=preview_seek,
                asset=asset,
                asset_names=asset_names,
                maximum_frames=maximum_frames,
                fps=fps,
                moment_count=len(moments),
                text_renderer=active_text_renderer,
            )
        except EditPathError as error:
            if active_text_renderer == "none":
                raise
            text_warnings.append(
                f"{active_text_renderer} labels failed at sequence {moment['sequence']}; replay continued without labels: {error}"
            )
            active_text_renderer = "none"
            _render_scene(
                ffmpeg,
                encoder,
                moment,
                scene,
                preview=preview,
                preview_seek=preview_seek,
                asset=asset,
                asset_names=asset_names,
                maximum_frames=maximum_frames,
                fps=fps,
                moment_count=len(moments),
                text_renderer=active_text_renderer,
            )
        scene_paths.append(scene)
        moment_reports.append(
            {
                "moment": moment["index"] + 1,
                "sequence": moment["sequence"],
                "event_id": moment["event"].get("event_id"),
                "event_type": moment["event_type"],
                "operation": moment["operation"],
                "duration_seconds": _scene_duration(moment),
                "state_sha256": _project_hash(state_event) if isinstance(state_event, dict) else None,
                "monitor": preview_mode,
            }
        )

    concat_file = work_dir / "events.ffconcat"
    concat_file.write_text(
        "ffconcat version 1.0\n" + "".join(f"file '{path.as_posix()}'\n" for path in scene_paths),
        encoding="utf-8",
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f".{output.stem}.tmp-{os.getpid()}{output.suffix}")
    temporary.unlink(missing_ok=True)
    _run(
        [
            ffmpeg,
            "-y",
            "-v",
            "error",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(concat_file),
            "-c",
            "copy",
            "-movflags",
            "+faststart",
            str(temporary),
        ],
        "editor replay assembly",
    )
    replace_with_retry(temporary, output)
    output_probe = probe(output)
    video_streams = [stream for stream in output_probe.get("streams", []) if stream.get("codec_type") == "video"]
    duration = float(output_probe.get("format", {}).get("duration", 0.0))
    # Every independently rendered scene is quantized to a whole output
    # frame.  Summing the requested fractional durations underestimates a
    # long replay by up to one frame per moment.
    expected_duration = sum(
        math.ceil(_scene_duration(moment) * PROCESS_FPS) / PROCESS_FPS
        for moment in moments
    )
    if not video_streams or abs(duration - expected_duration) > 0.75:
        raise GateError(
            "edit_process",
            f"editor replay failed structure gate: video_streams={len(video_streams)}, duration={duration:.3f}",
        )
    counts = {
        event_type: sum(moment["event_type"] == event_type for moment in moments)
        for event_type in sorted(REPLAY_EVENT_TYPES)
    }
    report = {
        "schema": "edit-path/process-video@2",
        "accepted": True,
        "source": "raw_ui_commands_gestures_and_exact_states",
        "training_view": {
            "layout": "full_nonlinear_editor",
            "panels": ["project_bin", "project_monitor", "effects_properties", "multitrack_timeline", "transport", "status"],
            "interaction_feedback": ["pointer_motion", "keyboard_shortcuts", "command_cards", "selected_clips", "semantic_apply_cards"],
        },
        "moments": len(moments),
        "states": counts.get("state.checkpoint", 0) + counts.get("state.diff", 0),
        "accepted_edits": len(accepted),
        "event_counts": counts,
        "duration_seconds": duration,
        "audio": "omitted_training_visualization",
        "text_overlays": active_text_renderer,
        "text_overlay_count": text_overlay_count,
        "training_ui_quality": "passed" if text_overlay_count >= len(moments) and not text_warnings and not preview_warnings else "degraded",
        "training_ui_warnings": text_warnings,
        "state_preview_quality": "passed" if not preview_warnings else "degraded",
        "state_preview_warnings": preview_warnings,
        "output": {"sha256": sha256_file(output), "probe": output_probe},
        "events": moment_reports,
    }
    return output, report
