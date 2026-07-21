#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Video Path Pilot contributors
# SPDX-License-Identifier: GPL-3.0-only
"""Create, annotate, launch, and finalize an editor sample workspace."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

from normalize_sample import build_sample
from validate_sample import validate_sample
from validate_video_path import validate as validate_raw


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def dump(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def command_init(args: argparse.Namespace) -> int:
    root = args.sample_dir.resolve()
    if root.exists():
        raise ValueError(f"refusing to overwrite existing sample directory: {root}")
    if not args.assets:
        raise ValueError("at least one source asset is required")
    for asset in args.assets:
        if not asset.is_file():
            raise ValueError(f"asset does not exist: {asset}")

    (root / "assets").mkdir(parents=True)
    (root / "output").mkdir()
    (root / "internal").mkdir()
    (root / "evidence").mkdir()

    assets = []
    for index, source in enumerate(args.assets, 1):
        asset_id = f"asset_{index:03d}"
        destination = root / "assets" / f"{asset_id}{source.suffix.lower()}"
        shutil.copy2(source.resolve(), destination)
        assets.append({
            "asset_id": asset_id,
            "original_filename": source.name,
            "file": destination.relative_to(root).as_posix(),
            "sha256": sha256(destination),
            "bytes": destination.stat().st_size,
        })

    metadata = {
        "collector_version": "0.1.0",
        "sample_id": args.sample_id or root.name,
        "created_at_utc": utc_now(),
        "status": "initialized",
        "prompt": args.prompt,
        "editor": {"editor_id": args.editor_id},
        "editor_plan": args.plan,
        "project": {
            "frame_rate": {"numerator": args.fps_num, "denominator": args.fps_den},
            "width": args.width,
            "height": args.height,
        },
        "assets": assets,
        "asset_binding_method": "first_use_order",
    }
    dump(root / "internal" / "collector-metadata.json", metadata)
    (root / "internal" / "rationale.jsonl").touch()
    print(f"created sample workspace: {root}")
    print("Import the files from its assets/ directory into Kdenlive in filename order.")
    print(f"Then launch with: {Path(__file__).name} launch {root}")
    return 0


def load_metadata(root: Path) -> dict:
    path = root / "internal" / "collector-metadata.json"
    if not path.is_file():
        raise ValueError(f"not a sample workspace: {root}")
    return json.loads(path.read_text(encoding="utf-8"))


def command_note(args: argparse.Namespace) -> int:
    root = args.sample_dir.resolve()
    load_metadata(root)
    note = {
        "note_id": str(uuid.uuid4()),
        "timestamp_utc": utc_now(),
        "reason": args.reason,
        "decision": args.decision,
    }
    with (root / "internal" / "rationale.jsonl").open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(note, ensure_ascii=False) + "\n")
    print(f"saved rationale note: {note['note_id']}")
    return 0


def command_launch(args: argparse.Namespace) -> int:
    root = args.sample_dir.resolve()
    metadata = load_metadata(root)
    raw = root / "evidence" / "raw-events.jsonl"
    if raw.exists():
        raise ValueError(f"raw recording already exists: {raw}")
    launcher = Path(__file__).with_name("run-video-path-pilot.sh")
    metadata["status"] = "recording"
    metadata["recording_started_at_utc"] = utc_now()
    dump(root / "internal" / "collector-metadata.json", metadata)
    return subprocess.call([str(launcher), str(raw)])


def copy_artifact(source: Path, destination: Path) -> None:
    if not source.is_file():
        raise ValueError(f"required file does not exist: {source}")
    if source.resolve() != destination.resolve():
        shutil.copy2(source.resolve(), destination)


def command_finalize(args: argparse.Namespace) -> int:
    root = args.sample_dir.resolve()
    metadata = load_metadata(root)
    raw = root / "evidence" / "raw-events.jsonl"
    raw_errors = validate_raw(raw)
    if raw_errors:
        raise ValueError("raw recording is invalid:\n  " + "\n  ".join(raw_errors))

    copy_artifact(args.project, root / "internal" / "final.kdenlive")
    suffix = args.output.suffix.lower() or ".mp4"
    final_video = root / "output" / f"final{suffix}"
    copy_artifact(args.output, final_video)
    metadata["status"] = "finalized"
    metadata["finalized_at_utc"] = utc_now()
    metadata["editor_review"] = args.review
    metadata["artifacts"] = {
        "final_video": final_video.relative_to(root).as_posix(),
        "final_video_sha256": sha256(final_video),
        "native_project": "internal/final.kdenlive",
        "native_project_sha256": sha256(root / "internal" / "final.kdenlive"),
        "raw_events": "evidence/raw-events.jsonl",
        "raw_events_sha256": sha256(raw),
    }
    dump(root / "internal" / "collector-metadata.json", metadata)
    dump(root / "sample.json", build_sample(root, metadata))
    errors = validate_sample(root / "sample.json", check_files=True)
    if errors:
        raise ValueError("generated sample failed validation:\n  " + "\n  ".join(errors))
    print(f"sample finalized and valid: {root / 'sample.json'}")
    return 0


def command_validate(args: argparse.Namespace) -> int:
    root = args.sample_dir.resolve()
    errors = validate_sample(root / "sample.json", check_files=True)
    if errors:
        for error in errors:
            print(error, file=sys.stderr)
        return 1
    print(f"valid sample: {root / 'sample.json'}")
    return 0


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    sub = result.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init", help="create a new sample workspace")
    init.add_argument("sample_dir", type=Path)
    init.add_argument("--sample-id")
    init.add_argument("--prompt", required=True)
    init.add_argument("--editor-id", required=True)
    init.add_argument("--plan", required=True, help="editor's high-level plan before editing")
    init.add_argument("--fps-num", type=int, default=25)
    init.add_argument("--fps-den", type=int, default=1)
    init.add_argument("--width", type=int, default=1920)
    init.add_argument("--height", type=int, default=1080)
    init.add_argument("assets", type=Path, nargs="+")
    init.set_defaults(function=command_init)

    note = sub.add_parser("note", help="record why an important editing decision was made")
    note.add_argument("sample_dir", type=Path)
    note.add_argument("--reason", required=True)
    note.add_argument("--decision", required=True)
    note.set_defaults(function=command_note)

    launch = sub.add_parser("launch", help="start the instrumented Kdenlive")
    launch.add_argument("sample_dir", type=Path)
    launch.set_defaults(function=command_launch)

    finalize = sub.add_parser("finalize", help="package and validate a completed sample")
    finalize.add_argument("sample_dir", type=Path)
    finalize.add_argument("--project", type=Path, required=True)
    finalize.add_argument("--output", type=Path, required=True)
    finalize.add_argument("--review", required=True, help="editor's final assessment")
    finalize.set_defaults(function=command_finalize)

    validate = sub.add_parser("validate", help="validate a finalized sample")
    validate.add_argument("sample_dir", type=Path)
    validate.set_defaults(function=command_validate)
    return result


def main() -> int:
    args = parser().parse_args()
    try:
        return args.function(args)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
