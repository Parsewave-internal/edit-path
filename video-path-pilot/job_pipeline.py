#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Video Path Pilot contributors
# SPDX-License-Identifier: GPL-3.0-only
"""Create assigned jobs and automatically package completed editing sessions."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import shutil
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

from normalize_sample import accepted_commits, build_sample, read_jsonl
from media_reconstruct import reconstruct as reconstruct_media
from validate_sample import validate_sample
from validate_video_path import validate as validate_raw

VIDEO_SUFFIXES = {".mp4", ".mov", ".mkv", ".webm"}
COLLECTION = {"clip": "clips", "track": "tracks", "composition": "compositions", "mix": "mixes", "master_effect": "master_effects"}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def dump(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def load_job(root: Path) -> dict:
    path = root / "job.json"
    if not path.is_file(): raise ValueError(f"job.json not found in {root}")
    job = json.loads(path.read_text(encoding="utf-8"))
    if job.get("schema_version") != "0.1.0": raise ValueError("unsupported job schema")
    if not str(job.get("task", {}).get("prompt", "")).strip(): raise ValueError("job prompt is missing")
    if not job.get("assets"): raise ValueError("job has no assets")
    ids: set[str] = set(); hashes: set[str] = set()
    for asset in job["assets"]:
        asset_id = asset.get("asset_id")
        if asset_id in ids: raise ValueError(f"duplicate asset ID: {asset_id}")
        ids.add(asset_id)
        if asset.get("sha256") in hashes: raise ValueError(f"duplicate asset content is ambiguous: {asset['file']}")
        hashes.add(asset.get("sha256"))
        file = root / asset["file"]
        if not file.is_file(): raise ValueError(f"job asset missing: {asset['file']}")
        if file.stat().st_size != asset.get("bytes") or sha256(file) != asset.get("sha256"):
            raise ValueError(f"job asset changed: {asset['file']}")
    return job


def create_job(args: argparse.Namespace) -> int:
    root = args.job_dir.resolve()
    if root.exists(): raise ValueError(f"refusing to overwrite: {root}")
    (root / "assets").mkdir(parents=True)
    assets = []
    for index, source in enumerate(args.assets, 1):
        if not source.is_file(): raise ValueError(f"asset missing: {source}")
        asset_id = f"asset_{index:03d}"
        target = root / "assets" / f"{asset_id}{source.suffix.lower()}"
        shutil.copy2(source, target)
        assets.append({"asset_id": asset_id, "file": target.relative_to(root).as_posix(),
                       "original_filename": source.name, "sha256": sha256(target), "bytes": target.stat().st_size})
    dump(root / "job.json", {"schema_version": "0.1.0", "job_id": args.job_id,
         "task": {"prompt": args.prompt},
         "project": {"frame_rate": {"numerator": args.fps_num, "denominator": args.fps_den},
                     "width": args.width, "height": args.height}, "assets": assets})
    print(f"created assigned job: {root}")
    return 0


def validate_job_command(args: argparse.Namespace) -> int:
    job = load_job(args.job_dir.resolve())
    print(json.dumps({"job_id": job["job_id"], "prompt": job["task"]["prompt"],
                      "project": job["project"], "asset_count": len(job["assets"])}))
    return 0


def properties(element: ET.Element) -> dict[str, str]:
    return {item.get("name", ""): item.text or "" for item in element.findall("property")}


def project_resources(project: Path) -> tuple[dict[str, Path], dict]:
    root = ET.parse(project).getroot()
    project_root = Path(root.get("root") or project.parent)
    resources: dict[str, Path] = {}
    for element in list(root.findall("chain")) + list(root.findall("producer")):
        props = properties(element)
        native_id, resource = props.get("kdenlive:id"), props.get("kdenlive:originalurl") or props.get("resource")
        if not native_id or not resource or props.get("mlt_service") in {"color", "qtext", "kdenlivetitle"}: continue
        candidate = Path(resource)
        if not candidate.is_absolute(): candidate = project_root / candidate
        candidate = candidate.resolve()
        previous = resources.get(native_id)
        if previous and previous != candidate: raise ValueError(f"Kdenlive bin ID {native_id} maps to multiple resources")
        resources[native_id] = candidate
    profile = root.find("profile")
    if profile is None: raise ValueError("Kdenlive project has no profile")
    settings = {"frame_rate": {"numerator": int(profile.get("frame_rate_num", "0")),
                                "denominator": int(profile.get("frame_rate_den", "0"))},
                "width": int(profile.get("width", "0")), "height": int(profile.get("height", "0"))}
    return resources, settings


def resolve_assets(job_root: Path, job: dict, project: Path) -> tuple[dict[str, str], list[dict]]:
    by_hash = {asset["sha256"]: asset["asset_id"] for asset in job["assets"]}
    resources, settings = project_resources(project)
    bindings: dict[str, str] = {}
    problems = []
    for native_id, resource in resources.items():
        if not resource.is_file():
            problems.append({"native_id": native_id, "resource": str(resource), "error": "missing"}); continue
        digest = sha256(resource)
        asset_id = by_hash.get(digest)
        if not asset_id:
            problems.append({"native_id": native_id, "resource": str(resource), "sha256": digest, "error": "not_in_job"}); continue
        bindings[native_id] = asset_id
    return bindings, problems


def canonical_hash(snapshot: dict) -> str:
    encoded = json.dumps(snapshot, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    return hashlib.sha256(encoded).hexdigest()


def apply_diff(snapshot: dict, diff: dict) -> None:
    for change in diff.get("changes", []):
        collection_name = COLLECTION.get(change.get("entity"))
        if not collection_name: continue
        collection = snapshot.setdefault(collection_name, [])
        native_id = change.get("native_id")
        position = next((index for index, item in enumerate(collection) if item.get("native_id") == native_id), None)
        kind = change.get("change")
        if kind == "removed" and position is not None: collection.pop(position)
        elif kind == "added": collection.append(copy.deepcopy(change["after"]))
        elif kind == "updated" and position is not None: collection[position] = copy.deepcopy(change["after"])
        if collection_name == "tracks":
            collection.sort(key=lambda item: item.get("position", 0))
        elif collection_name in {"clips", "compositions", "mixes"}:
            collection.sort(key=lambda item: (item.get("track_native_id", 0), item.get("timeline_start_frame", 0), item.get("native_id", 0)))
        else:
            collection.sort(key=lambda item: item.get("native_id", 0))
    if "duration_after" in diff: snapshot["duration_frames"] = diff["duration_after"]


def replay_report(raw_paths: list[Path]) -> dict:
    segments = []
    previous_final_hash = None
    all_passed = True
    for path in raw_paths:
        events = read_jsonl(path)
        checkpoint = next((event for event in events if event.get("event_type") == "state.checkpoint"), None)
        if not checkpoint:
            segments.append({"file": path.name, "status": "failed", "error": "missing_checkpoint"})
            all_passed = False; continue
        snapshot = copy.deepcopy(checkpoint["snapshot"])
        initial_hash = canonical_hash(snapshot)
        continuity = previous_final_hash is None or initial_hash == previous_final_hash
        steps = []
        for event in accepted_commits(events):
            apply_diff(snapshot, event.get("diff", {}))
            actual = canonical_hash(snapshot)
            passed = actual == event.get("after_hash")
            steps.append({"raw_event_id": event.get("event_id"), "expected_hash": event.get("after_hash"),
                          "replayed_hash": actual, "passed": passed})
            all_passed = all_passed and passed
        previous_final_hash = canonical_hash(snapshot)
        all_passed = all_passed and continuity
        segments.append({"file": path.name, "initial_hash": initial_hash, "continuity_with_previous": continuity,
                         "final_hash": previous_final_hash, "steps": steps, "status": "passed" if continuity and all(s["passed"] for s in steps) else "failed"})
    return {"schema_version": "0.1.0", "canonical_state_replay": "passed" if all_passed else "failed",
            "media_project_reconstruction": "not_implemented", "reconstructed_render": "not_implemented", "segments": segments}


def discover_one(session: Path, suffixes: set[str], label: str) -> Path:
    matches = [path for path in session.iterdir() if path.is_file() and path.suffix.lower() in suffixes]
    if len(matches) != 1: raise ValueError(f"expected exactly one {label} in {session}, found {len(matches)}")
    return matches[0]


def finalize_job(args: argparse.Namespace) -> int:
    job_root, session = args.job_dir.resolve(), args.session_dir.resolve()
    job = load_job(job_root)
    raw_paths = sorted(session.glob("raw-events-*.jsonl")) or sorted(session.glob("raw-events.jsonl"))
    if not raw_paths: raise ValueError("session contains no raw event files")
    for index, path in enumerate(raw_paths):
        errors = validate_raw(path, require_complete=index == len(raw_paths) - 1)
        if errors: raise ValueError(f"invalid recording segment {path.name}: " + "; ".join(errors))
    project = args.project.resolve() if args.project else discover_one(session, {".kdenlive"}, "Kdenlive project")
    output = args.output.resolve() if args.output else discover_one(session, VIDEO_SUFFIXES, "rendered video")
    _, project_settings = project_resources(project)
    if project_settings != job["project"]:
        raise ValueError(f"saved project profile {project_settings} does not match assigned profile {job['project']}")
    bindings, problems = resolve_assets(job_root, job, project)
    used_refs = {str(change.get(side, {}).get("asset_reference")) for path in raw_paths for event in read_jsonl(path)
                 if event.get("event_type") == "state.diff" for change in event.get("diff", {}).get("changes", [])
                 for side in ("before", "after") if change.get(side, {}).get("asset_reference") is not None}
    unresolved_used = sorted(ref for ref in used_refs if ref not in bindings)
    if unresolved_used: raise ValueError(f"project could not resolve used Kdenlive asset IDs: {', '.join(unresolved_used)}")

    sample_root = job_root / "completed-sample"
    if sample_root.exists(): raise ValueError(f"completed sample already exists: {sample_root}")
    for directory in ("assets", "output", "internal", "evidence", "validation"):
        (sample_root / directory).mkdir(parents=True, exist_ok=True)
    assets = []
    for asset in job["assets"]:
        source = job_root / asset["file"]
        target = sample_root / "assets" / source.name
        shutil.copy2(source, target)
        assets.append({**asset, "file": target.relative_to(sample_root).as_posix()})
    target_project = sample_root / "internal" / "final.kdenlive"; shutil.copy2(project, target_project)
    target_output = sample_root / "output" / f"editor-final{output.suffix.lower()}"; shutil.copy2(output, target_output)
    raw_artifacts = []
    for index, raw in enumerate(raw_paths, 1):
        target = sample_root / "evidence" / f"raw-events-{index:03d}.jsonl"; shutil.copy2(raw, target)
        raw_artifacts.append({"file": target.relative_to(sample_root).as_posix(), "sha256": sha256(target),
                              "termination": "normal" if index == len(raw_paths) else "crash"})
    metadata = {"sample_id": job["job_id"], "job_id": job["job_id"], "prompt": job["task"]["prompt"],
                "project": project_settings, "assets": assets, "native_asset_bindings": bindings,
                "asset_binding_method": "project_resource_sha256", "output_completion_confirmed": True,
                "artifacts": {"final_video": target_output.relative_to(sample_root).as_posix(),
                    "final_video_sha256": sha256(target_output), "native_project": "internal/final.kdenlive",
                    "native_project_sha256": sha256(target_project), "raw_events": raw_artifacts}}
    sample = build_sample(sample_root, metadata)
    sample["quality"]["project_asset_resolution_problems"] = problems
    dump(sample_root / "sample.json", sample)
    report = replay_report([sample_root / item["file"] for item in raw_artifacts])
    media = reconstruct_media(sample_root / "sample.json")
    report["media_project_reconstruction"] = media["status"]
    report["reconstructed_render"] = "created" if media["status"] in {"passed", "comparison_failed"} else "not_created"
    report["media"] = media
    dump(sample_root / "validation" / "reconstruction-report.json", report)
    sample["quality"]["canonical_reconstruction"] = report["canonical_state_replay"]
    sample["quality"]["media_reconstruction"] = media["status"]
    sample["quality"]["ready_for_client_review"] = report["canonical_state_replay"] == "passed" and media["status"] == "passed"
    if media["status"] in {"passed", "comparison_failed"}:
        reconstructed = sample_root / media["render"]
        sample["output"]["reconstructed_video"] = media["render"]
        sample["output"]["reconstructed_video_sha256"] = sha256(reconstructed)
    dump(sample_root / "sample.json", sample)
    errors = validate_sample(sample_root / "sample.json", check_files=True)
    if report["canonical_state_replay"] != "passed": errors.append("canonical reconstruction failed")
    if errors: raise ValueError("generated sample failed validation: " + "; ".join(errors))
    print(f"completed sample: {sample_root}")
    print(f"media reconstruction: {media['status']}")
    print(f"ready for client review: {str(sample['quality']['ready_for_client_review']).lower()}")
    return 0


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__); sub = result.add_subparsers(dest="command", required=True)
    create = sub.add_parser("create-job"); create.add_argument("job_dir", type=Path); create.add_argument("--job-id", required=True)
    create.add_argument("--prompt", required=True); create.add_argument("--fps-num", type=int, default=25); create.add_argument("--fps-den", type=int, default=1)
    create.add_argument("--width", type=int, default=1920); create.add_argument("--height", type=int, default=1080)
    create.add_argument("assets", type=Path, nargs="+"); create.set_defaults(function=create_job)
    check = sub.add_parser("validate-job"); check.add_argument("job_dir", type=Path); check.set_defaults(function=validate_job_command)
    finish = sub.add_parser("finalize"); finish.add_argument("job_dir", type=Path); finish.add_argument("session_dir", type=Path)
    finish.add_argument("--project", type=Path); finish.add_argument("--output", type=Path); finish.set_defaults(function=finalize_job)
    return result


def main() -> int:
    args = parser().parse_args()
    try: return args.function(args)
    except (OSError, ValueError, ET.ParseError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr); return 1


if __name__ == "__main__": raise SystemExit(main())
