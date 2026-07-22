#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Video Path Pilot contributors
# SPDX-License-Identifier: GPL-3.0-only
"""Create assigned jobs and automatically package completed editing sessions."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import shutil
import sys
import tempfile
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from edit_path.pipeline import process_session
from edit_path.io import write_jsonl
from edit_path.segments import assemble_segments, discover_segments
from normalize_sample import accepted_commits, build_sample, read_jsonl
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
    encoded = json.dumps(value, indent=2, ensure_ascii=False) + "\n"
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=path.parent,
            delete=False,
        ) as stream:
            temporary = Path(stream.name)
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.chmod(path.stat().st_mode & 0o777 if path.exists() else 0o644)
        os.replace(temporary, path)
    except Exception:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        raise


def mark_session_packaged(session: Path, completed: Path) -> None:
    manifest_path = session / "session.json"
    if not manifest_path.is_file():
        return
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["status"] = "packaged"
    manifest["updated_at_utc"] = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    dump(manifest_path, manifest)
    shutil.copy2(manifest_path, completed / "session.json")


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


def resource_path(value: str, project_root: Path, service: str = "") -> Path:
    value = value.strip()
    if value.startswith("timewarp:"):
        parts = value.split(":", 2)
        value = parts[2] if len(parts) == 3 else ""
    elif service == "timewarp" and ":" in value:
        # MLT serializes timewarp resources as "speed:/absolute/source";
        # the service name, rather than a timewarp: prefix, carries the type.
        value = value.split(":", 1)[1]
    if value.startswith("file:"):
        value = urllib.request.url2pathname(urllib.parse.urlparse(value).path)
        if os.name == "nt" and len(value) >= 3 and value[0] in "/\\" and value[2] == ":":
            value = value[1:]
    candidate = Path(os.path.expandvars(os.path.expanduser(value)))
    if not candidate.is_absolute():
        candidate = project_root / candidate
    return candidate.resolve()


def project_resources(project: Path) -> tuple[dict[str, Path], dict]:
    root = ET.parse(project).getroot()
    project_root = Path(root.get("root") or project.parent)
    if not project_root.is_absolute():
        project_root = project.parent / project_root
    project_root = project_root.resolve()
    resources: dict[str, Path] = {}
    for element in list(root.findall("chain")) + list(root.findall("producer")):
        props = properties(element)
        native_id, resource = props.get("kdenlive:id"), props.get("kdenlive:originalurl") or props.get("resource")
        if not native_id or not resource or props.get("mlt_service") in {"color", "qtext", "kdenlivetitle"}: continue
        candidate = resource_path(resource, project_root, props.get("mlt_service", ""))
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


def prepare_assets(session: Path, project: Path, source_assets: list[dict] | None = None, source_root: Path | None = None) -> tuple[list[dict], dict[str, str], list[dict]]:
    resources, _ = project_resources(project)
    by_digest: dict[str, tuple[dict, Path]] = {}
    if source_assets is not None and source_root is not None:
        for asset in source_assets:
            by_digest[asset["sha256"]] = (asset, source_root / asset["file"])

    asset_directory = session / "assets"
    asset_directory.mkdir(parents=True, exist_ok=True)
    records_by_digest: dict[str, dict] = {}
    bindings: dict[str, str] = {}
    problems: list[dict] = []
    for native_id, resource in sorted(resources.items()):
        if not resource.is_file():
            problems.append({"native_id": native_id, "resource": str(resource), "error": "missing"})
            continue
        digest = sha256(resource)
        selected = by_digest.get(digest)
        if source_assets is not None and selected is None:
            problems.append({"native_id": native_id, "resource": str(resource), "sha256": digest, "error": "not_in_job"})
            continue
        source = selected[1] if selected is not None else resource
        record = records_by_digest.get(digest)
        if record is None:
            asset_id = f"asset_{len(records_by_digest) + 1:03d}"
            target = asset_directory / f"{digest[:16]}-{resource.name}"
            if not target.exists():
                shutil.copy2(source, target)
            record = {
                "asset_id": asset_id,
                "file": target.relative_to(session).as_posix(),
                "original_filename": resource.name,
                "original_path": str(resource),
                "source": str(target.resolve()),
                "sha256": digest,
                "bytes": target.stat().st_size,
                "license_status": "pending",
                "bin_references": [],
            }
            records_by_digest[digest] = record
        record["bin_references"].append(native_id)
        bindings[native_id] = record["asset_id"]

    records = list(records_by_digest.values())
    for record in records:
        record["bin_references"].sort()
        if len(record["bin_references"]) == 1:
            record["bin_reference"] = record["bin_references"][0]
    return records, bindings, problems


def refresh_bundle_manifest(bundle: Path) -> None:
    path = bundle / "bundle-manifest.json"
    value = json.loads(path.read_text(encoding="utf-8"))
    value["files"] = {
        file.relative_to(bundle).as_posix(): {"sha256": sha256(file), "bytes": file.stat().st_size}
        for file in sorted(bundle.rglob("*"))
        if file.is_file() and file != path
    }
    dump(path, value)


def organize_dataset_item(bundle: Path, output_suffix: str) -> None:
    """Give a completed bundle a stable, role-oriented dataset layout."""
    moves = {
        "assets": "inputs/assets",
        f"reference/editor-final{output_suffix}": f"outputs/final{output_suffix}",
        "final.mp4": "edit-path/replay.mp4",
        "trajectory.jsonl": "edit-path/events.jsonl",
        "reconstructed-output.mp4": "verification/reconstructed.mp4",
        "reconstructed.kdenlive": "verification/reconstructed.kdenlive",
        "render-report.json": "verification/report.json",
        "checkpoint_refs": "verification/checkpoints",
        "raw-trajectory.jsonl": "provenance/assembled-events.jsonl",
        "evidence": "provenance/segments",
        "states": "provenance/states",
        "internal/final.kdenlive": "provenance/editor-project.kdenlive",
        "entity-map.json": "provenance/entity-map.json",
        "session.json": "provenance/session.json",
        "asset-manifest.json": "provenance/asset-bindings.json",
    }
    for source_name, destination_name in moves.items():
        source = bundle / source_name
        if not source.exists():
            continue
        destination = bundle / destination_name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source), str(destination))
    internal = bundle / "internal"
    if internal.is_dir() and not any(internal.iterdir()):
        internal.rmdir()
    reference = bundle / "reference"
    if reference.is_dir() and not any(reference.iterdir()):
        reference.rmdir()

    bindings_path = bundle / "provenance" / "asset-bindings.json"
    if bindings_path.is_file():
        manifest = json.loads(bindings_path.read_text(encoding="utf-8"))
        bindings = []
        for asset in manifest.get("assets", []):
            binding = {"asset_id": asset.get("asset_id")}
            for key in ("bin_reference", "bin_references", "license_status"):
                if key in asset:
                    binding[key] = asset[key]
            bindings.append(binding)
        dump(bindings_path, {"schema": "video-path/native-asset-bindings@1", "bindings": bindings})

    portable_project = bundle / "verification" / "reconstructed.kdenlive"
    if portable_project.is_file():
        portable_project.write_bytes(portable_project.read_bytes().replace(b"assets/", b"../inputs/assets/"))

    # Portable state/proxy paths live inside the canonical event stream.
    events_path = bundle / "edit-path" / "events.jsonl"
    if events_path.is_file():
        events = read_jsonl(events_path)
        for event in events:
            state = event.get("project_state")
            if isinstance(state, dict) and isinstance(state.get("path"), str):
                state["path"] = state["path"].replace("states/", "provenance/states/", 1)
            proxy = event.get("reference_proxy")
            if isinstance(proxy, dict) and isinstance(proxy.get("path"), str):
                proxy["path"] = proxy["path"].replace("checkpoint_refs/", "verification/checkpoints/", 1)
        write_jsonl(events_path, events)


def finalize_session(session: Path, project: Path, output: Path, job: dict, *, source_root: Path | None = None) -> Path:
    raw_paths = discover_segments(session)
    for index, path in enumerate(raw_paths):
        errors = validate_raw(path, require_complete=index == len(raw_paths) - 1)
        if errors: raise ValueError(f"invalid recording segment {path.name}: " + "; ".join(errors))
    assembly = assemble_segments(session, session / "trajectory.jsonl")
    _, project_settings = project_resources(project)
    if project_settings != job["project"]:
        raise ValueError(f"saved project profile {project_settings} does not match assigned profile {job['project']}")
    assets, bindings, problems = prepare_assets(session, project, job.get("assets"), source_root)
    used_refs = {str(change.get(side, {}).get("asset_reference")) for path in raw_paths for event in read_jsonl(path)
                 if event.get("event_type") == "state.diff" for change in event.get("diff", {}).get("changes", [])
                 for side in ("before", "after") if change.get(side, {}).get("asset_reference") is not None}
    unresolved_used = sorted(ref for ref in used_refs if ref not in bindings)
    if unresolved_used: raise ValueError(f"project could not resolve used Kdenlive asset IDs: {', '.join(unresolved_used)}")

    for directory in ("output", "internal", "evidence"):
        (session / directory).mkdir(parents=True, exist_ok=True)
    target_project = session / "internal" / "final.kdenlive"
    if project.resolve() != target_project.resolve(): shutil.copy2(project, target_project)
    target_output = session / "output" / f"final{output.suffix.lower()}"
    if output.resolve() != target_output.resolve(): shutil.copy2(output, target_output)
    raw_artifacts = []
    for index, raw in enumerate(raw_paths, 1):
        target = session / "evidence" / f"raw-events-{index:03d}.jsonl"
        if raw.resolve() != target.resolve(): shutil.copy2(raw, target)
        raw_artifacts.append({"file": target.relative_to(session).as_posix(), "sha256": sha256(target),
                              "termination": "normal" if index == len(raw_paths) else "crash"})
    metadata = {"sample_id": job["job_id"], "job_id": job["job_id"], "prompt": job["task"]["prompt"],
                "project": project_settings, "assets": assets, "native_asset_bindings": bindings,
                "asset_binding_method": "project_resource_sha256", "output_completion_confirmed": True,
                "artifacts": {"final_video": target_output.relative_to(session).as_posix(),
                    "final_video_sha256": sha256(target_output), "native_project": "internal/final.kdenlive",
                    "native_project_sha256": sha256(target_project), "raw_events": raw_artifacts}}
    sample = build_sample(session, metadata)
    if sample["task"].get("prompt") is None:
        sample["task"] = {"prompt": None, "prompt_status": "pending_internal_entry"}
        sample["quality"]["missing_requirements"] = ["task.prompt"]
    else:
        sample["task"]["prompt_status"] = "provided"
    sample["quality"]["project_asset_resolution_problems"] = problems
    sample["quality"]["segment_assembly"] = {**assembly, "path": "trajectory.jsonl"}
    dump(session / "sample.json", sample)
    dump(session / "asset-manifest.json", {"schema": "video-path/assets@2", "assets": assets})

    pipeline_output = session / "pipeline-output"
    result = process_session(session, pipeline_output)
    if result["status"] != "accepted":
        raise ValueError(f"production reconstruction rejected at {result.get('gate')}: {result.get('message')}")
    source_bundle = Path(result["path"])
    completed = session / "completed-sample"
    if completed.exists(): raise ValueError(f"completed sample already exists: {completed}")
    shutil.move(str(source_bundle), str(completed))
    organize_dataset_item(completed, output.suffix.lower())

    sample_path = completed / "sample.json"
    sample = json.loads(sample_path.read_text(encoding="utf-8"))
    report = json.loads((completed / "verification/report.json").read_text(encoding="utf-8"))
    final_name = f"outputs/final{output.suffix.lower()}"
    sample["output"]["video"] = final_name
    sample["output"]["sha256"] = sha256(completed / final_name)
    sample["output"]["edit_process_video"] = "edit-path/replay.mp4"
    sample["output"]["edit_process_video_sha256"] = sha256(completed / "edit-path/replay.mp4")
    sample["output"]["reconstructed_video"] = "verification/reconstructed.mp4"
    sample["output"]["reconstructed_video_sha256"] = sha256(completed / "verification/reconstructed.mp4")
    sample["inputs"]["assets"] = [
        {**asset, "file": asset["file"].replace("assets/", "inputs/assets/", 1)}
        for asset in sample["inputs"]["assets"]
    ]
    sample["evidence"]["native_project"] = "provenance/editor-project.kdenlive"
    for raw in sample["evidence"].get("raw_events", []):
        raw["file"] = raw["file"].replace("evidence/", "provenance/segments/", 1)
    sample["quality"]["segment_assembly"]["path"] = "edit-path/events.jsonl"
    sample["quality"]["canonical_reconstruction"] = "passed"
    sample["quality"]["media_reconstruction"] = "passed" if report.get("final", {}).get("accepted") else "failed"
    sample["quality"]["ready_for_client_review"] = sample["quality"]["media_reconstruction"] == "passed" and bool(sample["task"].get("prompt"))
    dump(sample_path, sample)
    errors = validate_sample(sample_path, check_files=True)
    if errors: raise ValueError("generated sample failed validation: " + "; ".join(errors))
    mark_session_packaged(session, completed)
    refresh_bundle_manifest(completed)
    print(f"completed sample: {completed}")
    print(f"media reconstruction: {sample['quality']['media_reconstruction']}")
    print(f"ready for client review: {str(sample['quality']['ready_for_client_review']).lower()}")
    return completed


def finalize_job(args: argparse.Namespace) -> int:
    job_root, session = args.job_dir.resolve(), args.session_dir.resolve()
    job = load_job(job_root)
    project = args.project.resolve() if args.project else discover_one(session, {".kdenlive"}, "Kdenlive project")
    output = args.output.resolve() if args.output else discover_one(session, VIDEO_SUFFIXES, "rendered video")
    finalize_session(session, project, output, job, source_root=job_root)
    return 0


def finalize_freeform(args: argparse.Namespace) -> int:
    session = args.session_dir.resolve()
    if (session / "completed-sample").exists(): raise ValueError("this session already has a completed sample")
    project = args.project.resolve() if args.project else discover_one(session, {".kdenlive"}, "Kdenlive project")
    output = args.output.resolve() if args.output else discover_one(session, VIDEO_SUFFIXES, "rendered video")
    resources, settings = project_resources(project)
    if not any(resource.is_file() for resource in resources.values()): raise ValueError("saved project contains no resolvable media resources")
    job = {"schema_version": "0.1.0", "job_id": session.name,
           "task": {"prompt": None}, "project": settings}
    target_sample = finalize_session(session, project, output, job)
    print(f"freeform sample generated: {target_sample}")
    print("task prompt: pending internal entry")
    return 0


def attach_prompt(args: argparse.Namespace) -> int:
    sample_path = args.sample_dir.resolve() / "sample.json"
    if not sample_path.is_file(): raise ValueError(f"sample.json not found: {sample_path}")
    sample = json.loads(sample_path.read_text(encoding="utf-8")); prompt = args.prompt.strip()
    if not prompt: raise ValueError("prompt must not be empty")
    sample["task"] = {"prompt": prompt, "prompt_status": "provided"}
    sample["quality"]["missing_requirements"] = []
    sample["quality"]["ready_for_client_review"] = (sample["quality"].get("canonical_reconstruction") == "passed"
                                                      and sample["quality"].get("media_reconstruction") == "passed")
    dump(sample_path, sample)
    errors = validate_sample(sample_path, check_files=True)
    if errors: raise ValueError("sample failed after prompt attachment: " + "; ".join(errors))
    refresh_bundle_manifest(args.sample_dir.resolve())
    print(f"prompt attached; ready for client review: {str(sample['quality']['ready_for_client_review']).lower()}")
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
    freeform = sub.add_parser("finalize-freeform"); freeform.add_argument("session_dir", type=Path)
    freeform.add_argument("--project", type=Path); freeform.add_argument("--output", type=Path); freeform.set_defaults(function=finalize_freeform)
    prompt = sub.add_parser("attach-prompt"); prompt.add_argument("sample_dir", type=Path); prompt.add_argument("--prompt", required=True)
    prompt.set_defaults(function=attach_prompt)
    return result


def main() -> int:
    args = parser().parse_args()
    try: return args.function(args)
    except (OSError, ValueError, ET.ParseError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr); return 1


if __name__ == "__main__": raise SystemExit(main())
