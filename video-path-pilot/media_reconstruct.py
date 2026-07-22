#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Video Path Pilot contributors
# SPDX-License-Identifier: GPL-3.0-only
"""Reconstruct and render the supported canonical edit subset with MLT."""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

from edit_path.reconstruct import select_video_encoder


class UnsupportedEdit(ValueError):
    pass


def prop(parent: ET.Element, name: str, value: object) -> None:
    element = ET.SubElement(parent, "property", {"name": name}); element.text = str(value)


def assert_supported(state: dict) -> None:
    if state.get("compositions") or state.get("mixes"):
        raise UnsupportedEdit("transitions and mixes are not yet supported by the media adapter")
    for master in state.get("master_effects", []):
        if master.get("effects"): raise UnsupportedEdit("master effects are not yet supported")
    for track in state.get("tracks", []):
        if track.get("effects"): raise UnsupportedEdit("track effects are not yet supported")
    for clip in state.get("clips", []):
        if clip.get("effects"): raise UnsupportedEdit("clip effects and keyframes are not yet supported")
        if clip.get("speed", 1) != 1: raise UnsupportedEdit("speed changes are not yet supported")


def build_mlt(sample_path: Path, destination: Path) -> None:
    sample = json.loads(sample_path.read_text(encoding="utf-8")); root_dir = sample_path.parent
    state = sample["edit_path"]["final_state"]; assert_supported(state)
    project = sample["project"]; rate = project["frame_rate"]; duration = max(1, state.get("duration_frames", 1))
    assets = {asset["asset_id"]: (root_dir / asset["file"]).resolve() for asset in sample["inputs"]["assets"]}
    mlt = ET.Element("mlt", {"LC_NUMERIC": "C", "producer": "main", "version": "7.0.0"})
    ET.SubElement(mlt, "profile", {"frame_rate_num": str(rate["numerator"]), "frame_rate_den": str(rate["denominator"]),
        "width": str(project["width"]), "height": str(project["height"]), "progressive": "1",
        "sample_aspect_num": "1", "sample_aspect_den": "1", "display_aspect_num": str(project["width"]),
        "display_aspect_den": str(project["height"]), "colorspace": "709"})
    black = ET.SubElement(mlt, "producer", {"id": "background", "in": "0", "out": str(duration - 1)})
    prop(black, "resource", "black"); prop(black, "mlt_service", "color"); prop(black, "mlt_image_format", "rgba")
    background = ET.SubElement(mlt, "playlist", {"id": "background_playlist"})
    ET.SubElement(background, "entry", {"producer": "background", "in": "0", "out": str(duration - 1)})

    tracks = sorted(state.get("tracks", []), key=lambda item: item.get("position", 0))
    clips_by_track: dict[str, list[dict]] = {}
    for clip in state.get("clips", []): clips_by_track.setdefault(clip["track_id"], []).append(clip)
    playlist_ids = []
    for track_index, track in enumerate(tracks, 1):
        playlist_id = f"playlist_{track_index}"; playlist_ids.append((playlist_id, track.get("kind", "video")))
        playlist = ET.SubElement(mlt, "playlist", {"id": playlist_id}); cursor = 0
        for clip_index, clip in enumerate(sorted(clips_by_track.get(track["track_id"], []), key=lambda item: item["timeline_start_frame"]), 1):
            start = clip["timeline_start_frame"]
            if start < cursor: raise UnsupportedEdit("overlapping clips on one track are not yet supported")
            if start > cursor: ET.SubElement(playlist, "blank", {"length": str(start - cursor)})
            producer_id = f"producer_{track_index}_{clip_index}"
            producer = ET.SubElement(mlt, "producer", {"id": producer_id, "in": str(clip["source_start_frame"]), "out": str(clip["source_end_frame"])})
            resource = assets.get(clip.get("asset_id"))
            if resource is None: raise ValueError(f"asset is missing from sample: {clip.get('asset_id')}")
            prop(producer, "resource", resource); prop(producer, "mlt_service", "avformat")
            # MLT XML resolves producer references in document order.
            mlt.remove(producer)
            first_playlist = next(index for index, child in enumerate(list(mlt)) if child.tag == "playlist")
            mlt.insert(first_playlist, producer)
            ET.SubElement(playlist, "entry", {"producer": producer_id, "in": str(clip["source_start_frame"]), "out": str(clip["source_end_frame"])})
            cursor = start + clip["duration_frames"]

    tractor = ET.SubElement(mlt, "tractor", {"id": "main", "in": "0", "out": str(duration - 1)})
    ET.SubElement(tractor, "track", {"producer": "background_playlist"})
    for index, (playlist_id, kind) in enumerate(playlist_ids, 1):
        attributes = {"producer": playlist_id, "hide": "audio" if kind == "video" else "video"}
        ET.SubElement(tractor, "track", attributes)
        transition = ET.SubElement(tractor, "transition", {"id": f"transition_{index}"})
        prop(transition, "a_track", 0); prop(transition, "b_track", index)
        prop(transition, "mlt_service", "qtblend" if kind == "video" else "mix")
        prop(transition, "always_active", 1)
        if kind == "audio":
            prop(transition, "accepts_blanks", 1); prop(transition, "sum", 1)
        else:
            prop(transition, "compositing", 0); prop(transition, "distort", 0); prop(transition, "rotate_center", 0)
    ET.indent(mlt)
    ET.ElementTree(mlt).write(destination, encoding="utf-8", xml_declaration=True)


def ffprobe(path: Path) -> dict:
    result = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration:stream=codec_type,width,height,r_frame_rate",
                             "-of", "json", str(path)], check=True, capture_output=True, text=True)
    return json.loads(result.stdout)


def compare_media(editor: Path, reconstructed: Path, editor_probe: dict, reconstructed_probe: dict) -> dict:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg: raise RuntimeError("ffmpeg executable was not found")
    video = subprocess.run([ffmpeg, "-hide_banner", "-i", str(editor), "-i", str(reconstructed),
                            "-lavfi", "[0:v][1:v]ssim", "-f", "null", "-"], capture_output=True, text=True)
    match = re.search(r"All:([0-9.]+)", video.stderr)
    if not match: raise RuntimeError("FFmpeg did not report video SSIM")
    ssim = float(match.group(1))
    editor_audio = any(stream.get("codec_type") == "audio" for stream in editor_probe.get("streams", []))
    reconstructed_audio = any(stream.get("codec_type") == "audio" for stream in reconstructed_probe.get("streams", []))
    audio_psnr = None
    if editor_audio and reconstructed_audio:
        audio = subprocess.run([ffmpeg, "-hide_banner", "-i", str(editor), "-i", str(reconstructed),
                                "-lavfi", "[0:a][1:a]apsnr", "-f", "null", "-"], capture_output=True, text=True)
        values = [float(value) for value in re.findall(r"PSNR ch\d+: ([0-9.]+) dB", audio.stderr)]
        if values: audio_psnr = min(values)
    editor_video = next(stream for stream in editor_probe["streams"] if stream.get("codec_type") == "video")
    reconstructed_video = next(stream for stream in reconstructed_probe["streams"] if stream.get("codec_type") == "video")
    editor_duration = float(editor_probe["format"]["duration"]); reconstructed_duration = float(reconstructed_probe["format"]["duration"])
    profile_match = (editor_video.get("width"), editor_video.get("height"), editor_video.get("r_frame_rate")) == (
                     reconstructed_video.get("width"), reconstructed_video.get("height"), reconstructed_video.get("r_frame_rate"))
    duration_delta = abs(editor_duration - reconstructed_duration)
    audio_structure_match = editor_audio == reconstructed_audio
    passed = profile_match and duration_delta <= 0.05 and ssim >= 0.95 and audio_structure_match and (audio_psnr is None or audio_psnr >= 40.0)
    return {"profile_match": profile_match, "duration_delta_seconds": duration_delta, "video_ssim": ssim,
            "video_ssim_threshold": 0.95, "audio_psnr_db": audio_psnr, "audio_psnr_threshold_db": 40.0,
            "audio_structure_match": audio_structure_match, "passed": passed}


def reconstruct(sample_path: Path) -> dict:
    root = sample_path.parent; validation = root / "validation"; output = root / "output"
    validation.mkdir(exist_ok=True); output.mkdir(exist_ok=True)
    mlt_path = validation / "reconstructed.mlt"; render = output / "reconstructed.mp4"
    report = {"adapter": "mlt-basic-cut-v0.1", "project": str(mlt_path.relative_to(root)), "render": str(render.relative_to(root))}
    try:
        build_mlt(sample_path, mlt_path)
        melt = shutil.which("melt-7") or shutil.which("melt")
        if not melt: raise RuntimeError("melt executable was not found")
        video_encoder = select_video_encoder()
        encoder_options = [f"vcodec={video_encoder}"]
        if video_encoder == "libx264": encoder_options.extend(["crf=18", "preset=medium"])
        elif video_encoder == "libopenh264": encoder_options.extend(["vb=8M", "g=50"])
        else: encoder_options.extend(["qscale=2", "g=50"])
        subprocess.run([melt, str(mlt_path), "-consumer", f"avformat:{render}", *encoder_options, "acodec=aac", "real_time=-1"],
                       check=True, capture_output=True, text=True)
        editor = root / json.loads(sample_path.read_text())["output"]["video"]
        reconstructed_probe, editor_probe = ffprobe(render), ffprobe(editor)
        comparison = compare_media(editor, render, editor_probe, reconstructed_probe)
        report.update({"status": "passed" if comparison["passed"] else "comparison_failed",
                       "reconstructed_probe": reconstructed_probe, "editor_probe": editor_probe, "comparison": comparison})
    except UnsupportedEdit as exc:
        report.update({"status": "unsupported", "reason": str(exc)})
    except (OSError, RuntimeError, subprocess.CalledProcessError, json.JSONDecodeError, ET.ParseError) as exc:
        report.update({"status": "failed", "reason": str(exc)})
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__); parser.add_argument("sample", type=Path); args = parser.parse_args()
    report = reconstruct(args.sample.resolve()); print(json.dumps(report, indent=2)); return 0 if report["status"] == "passed" else 1


if __name__ == "__main__": raise SystemExit(main())
