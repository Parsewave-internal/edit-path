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
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from edit_path.io import find_trajectory, read_jsonl
from edit_path.errors import EditPathError
from edit_path.pipeline import build_dataset_index, process_session, semantic_activity, validate_event_envelope
from edit_path.reconstruct import materialize_project, render_session
from edit_path.state import resolve_accepted_branch, validate_state_transitions

from normalize_sample import build_sample
from job_pipeline import embedded_project_assets
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


def bind_project_assets(project: Path, root: Path, assets: list[dict]) -> list[dict]:
    """Resolve Kdenlive bin IDs to collector assets using exact files/hashes."""
    xml = ET.fromstring(project.read_bytes())
    by_path = {(root / asset["file"]).resolve(): asset for asset in assets}
    by_digest = {asset["sha256"]: asset for asset in assets}
    bindings: dict[str, str] = {}
    for producer in xml.iter("producer"):
        properties = {
            child.get("name"): child.text or ""
            for child in producer.findall("property")
            if child.get("name")
        }
        bin_reference = properties.get("kdenlive:id")
        if not bin_reference:
            continue
        candidates = [properties.get("kdenlive:originalurl"), properties.get("resource")]
        matched: dict | None = None
        for candidate_value in candidates:
            if not candidate_value or candidate_value.startswith(("color:", "colour:", "<", "kdenlivetitle:")):
                continue
            candidate = candidate_value
            if candidate.startswith("file://"):
                candidate = urllib.parse.unquote(urllib.parse.urlparse(candidate).path)
            if candidate.startswith("timewarp:"):
                candidate = candidate.split(":", 2)[-1]
            elif properties.get("mlt_service") == "timewarp" and ":" in candidate:
                candidate = candidate.split(":", 1)[1]
            value = Path(candidate)
            if not value.is_absolute():
                value = project.parent / value
            resolved = value.resolve()
            if resolved in by_path:
                matched = by_path[resolved]
                break
            if resolved.is_file():
                digest_match = by_digest.get(sha256(resolved))
                if digest_match and resolved.stat().st_size == digest_match["bytes"]:
                    matched = digest_match
                    break
        if matched:
            previous = bindings.get(bin_reference)
            if previous and previous != matched["asset_id"]:
                raise ValueError(f"bin reference {bin_reference} maps to multiple assets")
            bindings[bin_reference] = matched["asset_id"]
    result = []
    for asset in assets:
        value = dict(asset)
        references = sorted(reference for reference, asset_id in bindings.items() if asset_id == asset["asset_id"])
        if len(references) != 1:
            raise ValueError(f"could not bind exactly one Kdenlive bin reference for {asset['asset_id']}")
        value["bin_reference"] = references[0]
        result.append(value)
    return result


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
    seen_digests: set[str] = set()
    for source in args.assets:
        digest = sha256(source)
        if digest in seen_digests:
            continue
        seen_digests.add(digest)
        asset_id = f"asset_{len(assets) + 1:03d}"
        destination = root / "assets" / f"{digest}{source.suffix.lower()}"
        shutil.copy2(source.resolve(), destination)
        assets.append({
            "asset_id": asset_id,
            "original_filename": source.name,
            "file": destination.relative_to(root).as_posix(),
            "sha256": digest,
            "bytes": destination.stat().st_size,
            "license_status": "pending",
        })

    metadata = {
        "collector_version": "0.3.0",
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
        "asset_binding_method": "kdenlive_bin_reference",
    }
    dump(root / "internal" / "collector-metadata.json", metadata)
    (root / "internal" / "rationale.jsonl").touch()
    print(f"created sample workspace: {root}")
    print("Import the files from its assets/ directory into Kdenlive.")
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

    if metadata.get("collector_version") == "0.3.0":
        metadata["assets"] = bind_project_assets(args.project.resolve(), root, metadata["assets"])
        metadata["embedded_project_assets"] = embedded_project_assets(args.project.resolve())
    copy_artifact(args.project, root / "internal" / "final.kdenlive")
    events = read_jsonl(raw)
    contexts = [event.get("context") for event in events if event.get("event_type") == "project.context"]
    if contexts:
        metadata["project"]["recorded_context"] = contexts[-1]
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
    dump(root / "asset-manifest.json", {"schema": "video-path/assets@2", "assets": metadata["assets"]})
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


def command_inspect(args: argparse.Namespace) -> int:
    root = args.sample_dir.resolve()
    events = read_jsonl(find_trajectory(root))
    envelope = validate_event_envelope(events, require_complete=False)
    _, states = validate_state_transitions(events)
    branch = resolve_accepted_branch(events, require_targets=any(event.get("schema_version") == "0.3.0" for event in events))
    print(json.dumps({
        "session": envelope,
        "state_events": len(states),
        "accepted_commits": len(branch.accepted),
        "final_hash": branch.final_hash,
        "semantic_activity": semantic_activity(branch.accepted),
    }, indent=2, sort_keys=True))
    return 0


def command_reconstruct(args: argparse.Namespace) -> int:
    root = args.sample_dir.resolve()
    output = args.output.resolve() if args.output else None
    result = materialize_project(root, output) if args.project_only else render_session(root, output, melt_binary=args.melt)
    print(result)
    return 0


def command_process(args: argparse.Namespace) -> int:
    result = process_session(
        args.sample_dir.resolve(),
        args.output_root.resolve(),
        minimum_ssim=args.minimum_ssim,
        minimum_final_ssim=args.minimum_final_ssim,
        minimum_commits=args.minimum_commits,
        minimum_changed_entities=args.minimum_changed_entities,
        require_license=args.require_license,
        require_complete=not args.allow_partial,
        melt_binary=args.melt,
        runtime_lock=args.runtime_lock.resolve() if args.runtime_lock else None,
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["status"] == "accepted" else 2


def command_index(args: argparse.Namespace) -> int:
    print(json.dumps(build_dataset_index(args.output_root.resolve()), indent=2, sort_keys=True))
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

    inspect = sub.add_parser("inspect", help="validate state/hash chains and show the accepted branch")
    inspect.add_argument("sample_dir", type=Path)
    inspect.set_defaults(function=command_inspect)

    reconstruct = sub.add_parser("reconstruct", help="materialize the final project or render its MP4")
    reconstruct.add_argument("sample_dir", type=Path)
    reconstruct.add_argument("--output", type=Path)
    reconstruct.add_argument("--project-only", action="store_true")
    reconstruct.add_argument("--melt")
    reconstruct.set_defaults(function=command_reconstruct)

    process = sub.add_parser("process", help="run all gates and route to accepted/ or quarantine/")
    process.add_argument("sample_dir", type=Path)
    process.add_argument("output_root", type=Path)
    process.add_argument("--minimum-ssim", type=float, default=0.995)
    process.add_argument("--minimum-final-ssim", type=float, default=0.99)
    process.add_argument("--minimum-commits", type=int, default=1)
    process.add_argument("--minimum-changed-entities", type=int, default=1)
    process.add_argument("--allow-partial", action="store_true")
    process.add_argument("--require-license", action="store_true", help="optional late publication gate")
    process.add_argument("--melt")
    process.add_argument("--runtime-lock", type=Path)
    process.set_defaults(function=command_process)

    index = sub.add_parser("index", help="rebuild the accepted dataset index")
    index.add_argument("output_root", type=Path)
    index.set_defaults(function=command_index)
    return result


def main() -> int:
    args = parser().parse_args()
    try:
        return args.function(args)
    except (EditPathError, OSError, ValueError, json.JSONDecodeError, ET.ParseError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
