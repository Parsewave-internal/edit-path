# SPDX-FileCopyrightText: 2026 Edit Path contributors
# SPDX-License-Identifier: GPL-3.0-only

from __future__ import annotations

import json
import os
import shutil
import tempfile
import copy
import hashlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .assets import load_manifest, remap_project_assets, verify_assets
from .errors import EditPathError, GateError
from .io import RAW_SCHEMA_VERSIONS, event_sequence, find_trajectory, read_jsonl, safe_relative, sha256_file, write_json, write_jsonl
from .reconstruct import render_event, render_session, state_reference
from .runtime import runtime_fingerprint, verify_runtime_lock
from .state import load_state_reference, resolve_accepted_branch, validate_action_semantics, validate_state_transitions
from .validate import probe, validate_render


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def validate_event_envelope(events: list[dict[str, Any]], *, require_complete: bool = True) -> dict[str, Any]:
    session_ids = {event.get("session_id") for event in events}
    if len(session_ids) != 1 or None in session_ids:
        raise GateError("session", "trajectory must contain exactly one session_id")
    expected = 1
    ids: set[str] = set()
    schema_versions: set[str] = set()
    for event in events:
        schema_version = event.get("schema_version")
        if schema_version not in RAW_SCHEMA_VERSIONS:
            raise GateError("session", f"unsupported schema_version {schema_version!r}", event_sequence(event))
        schema_versions.add(schema_version)
        sequence = event_sequence(event)
        if sequence != expected:
            raise GateError("session", f"expected sequence {expected}, got {sequence!r}", sequence)
        expected += 1
        event_id = event.get("event_id")
        if not isinstance(event_id, str) or event_id in ids:
            raise GateError("session", "event_id is missing or duplicated", sequence)
        ids.add(event_id)
    if len(schema_versions) != 1:
        raise GateError("session", f"session mixes schema versions: {sorted(schema_versions)}")
    if events[0].get("event_type") != "session.start":
        raise GateError("session", "first event must be session.start", event_sequence(events[0]))
    complete = events[-1].get("event_type") == "session.end"
    if sum(event.get("event_type") == "session.end" for event in events) > 1:
        raise GateError("session", "session contains more than one session.end")
    aborted = next((event for event in events if event.get("event_type") == "session.abort"), None)
    if aborted is not None:
        raise GateError("session", f"session was explicitly aborted: {aborted.get('reason', 'unknown')}", event_sequence(aborted))
    if require_complete and not complete:
        raise GateError("session", "session is incomplete: final event is not session.end", event_sequence(events[-1]))
    if complete and events[-1].get("schema_version") == "0.3.0" and events[-1].get("state_sidecars_complete") is not True:
        raise GateError("state_sidecars", "v0.3 session did not durably finish its state and checkpoint sidecars", event_sequence(events[-1]))
    if "0.3.0" in schema_versions:
        contexts = [event for event in events if event.get("event_type") == "project.context"]
        if len(contexts) != 1:
            raise GateError("session", f"v0.3 session requires exactly one project.context; found {len(contexts)}")
        context = contexts[0].get("context")
        required_context = {
            "project_id", "fps_numerator", "fps_denominator", "width", "height",
            "sample_aspect_numerator", "sample_aspect_denominator",
            "display_aspect_numerator", "display_aspect_denominator", "colorspace",
            "progressive", "bottom_field_first", "audio_channels", "audio_sample_rate",
            "kdenlive_version", "kdenlive_build", "mlt_version",
        }
        if not isinstance(context, dict) or not required_context <= context.keys():
            missing = sorted(required_context - set(context or {}))
            raise GateError("project_context", f"v0.3 project.context is incomplete; missing {missing}", event_sequence(contexts[0]))
    return {"session_id": next(iter(session_ids)), "schema_version": next(iter(schema_versions)), "complete": complete, "events": len(events)}


def semantic_activity(accepted: list[dict[str, Any]], *, minimum_commits: int = 1, minimum_changed_entities: int = 1) -> dict[str, Any]:
    commits = len(accepted)
    entity_keys: set[tuple[str, str]] = set()
    duration_delta_frames = 0
    for event in accepted:
        diff = event.get("diff", {})
        for change in diff.get("changes", []):
            value = change.get("after") or change.get("before") or {}
            identity = value.get("entity_id", change.get("native_id"))
            entity_keys.add((str(change.get("entity")), str(identity)))
        if isinstance(diff.get("duration_before"), int) and isinstance(diff.get("duration_after"), int):
            duration_delta_frames += abs(diff["duration_after"] - diff["duration_before"])
    changed_entities = len(entity_keys)
    if duration_delta_frames and not changed_entities:
        changed_entities = 1
    if commits < minimum_commits:
        raise GateError("semantic_activity", f"requires at least {minimum_commits} accepted commits; found {commits}")
    if changed_entities < minimum_changed_entities:
        raise GateError(
            "semantic_activity",
            f"requires at least {minimum_changed_entities} changed entities; found {changed_entities}",
        )
    return {
        "accepted_commits": commits,
        "unique_changed_entities": changed_entities,
        "duration_delta_frames": duration_delta_frames,
    }


def validate_project_state_sidecars(
    session_dir: Path,
    trajectory: Path,
    events: list[dict[str, Any]],
    *,
    require_exact: bool,
) -> dict[str, Any]:
    current_hash: str | None = None
    checked = 0
    for event in events:
        if event.get("event_type") not in {"state.checkpoint", "state.diff"}:
            continue
        reference = state_reference(event)
        sequence = event_sequence(event)
        if reference is None:
            if require_exact:
                raise GateError("state_sidecars", "v0.3 state event has no exact project_state", sequence)
            continue
        base = session_dir if reference.get("base") == "session" else trajectory.parent
        load_state_reference(reference, base)
        digest = reference.get("sha256")
        if event.get("event_type") == "state.checkpoint":
            if current_hash is not None and digest != current_hash:
                raise GateError("project_hash_chain", "checkpoint exact state does not match the preceding project state", sequence)
        else:
            before_hash = event.get("project_before_hash")
            after_hash = event.get("project_after_hash")
            if require_exact and before_hash != current_hash:
                raise GateError("project_hash_chain", f"project_before_hash does not continue {current_hash}", sequence)
            if require_exact and after_hash != digest:
                raise GateError("project_hash_chain", "project_after_hash does not match project_state", sequence)
        current_hash = digest
        checked += 1
    if require_exact and checked == 0:
        raise GateError("state_sidecars", "v0.3 session contains no exact project states")
    return {"states_checked": checked, "final_project_hash": current_hash}


def validate_stable_entities(events: list[dict[str, Any]], *, required: bool) -> int:
    if not required:
        return 0
    checked = 0
    for event in events:
        sequence = event_sequence(event)
        values: list[tuple[str, dict[str, Any]]] = []
        if event.get("event_type") == "state.checkpoint":
            snapshot = event.get("snapshot", {})
            for plural, singular in (
                ("tracks", "track"),
                ("clips", "clip"),
                ("compositions", "composition"),
                ("mixes", "mix"),
                ("master_effects", "master_effect"),
            ):
                values.extend((singular, value) for value in snapshot.get(plural, []) if isinstance(value, dict))
        elif event.get("event_type") == "state.diff":
            for change in event.get("diff", {}).get("changes", []):
                for side in ("before", "after"):
                    value = change.get(side)
                    if isinstance(value, dict):
                        values.append((str(change.get("entity")), value))
        for entity, value in values:
            if not isinstance(value.get("entity_id"), str) or not value["entity_id"]:
                raise GateError("stable_entities", f"v0.3 {entity} is missing entity_id", sequence)
            if entity == "clip" and (not isinstance(value.get("asset_id"), str) or not value["asset_id"]):
                raise GateError("stable_entities", "v0.3 clip is missing asset_id", sequence)
            checked += 1
    return checked


def checkpoint_reference(session_dir: Path, event: dict[str, Any]) -> Path | None:
    explicit = event.get("reference_proxy")
    if isinstance(explicit, dict):
        path = explicit.get("path")
        if not isinstance(path, str):
            return None
        base = find_trajectory(session_dir).parent if explicit.get("base") == "trajectory" else session_dir
        return base / path
    sequence = event_sequence(event)
    for suffix in (".mp4", ".mkv"):
        candidate = session_dir / "checkpoint_refs" / f"{sequence:08d}{suffix}"
        if candidate.is_file():
            return candidate
    return None


def validate_checkpoints(
    session_dir: Path,
    events: list[dict[str, Any]],
    work_dir: Path,
    *,
    minimum_ssim: float,
    melt_binary: str | None,
    require_references: bool,
) -> list[dict[str, Any]]:
    results = []
    for event in events:
        if event.get("event_type") != "state.checkpoint" or state_reference(event) is None:
            continue
        reference = checkpoint_reference(session_dir, event)
        if reference is None:
            if require_references:
                raise GateError("checkpoint_ssim", "v0.3 checkpoint has no independent reference proxy", event_sequence(event))
            results.append({"sequence": event_sequence(event), "status": "skipped", "reason": "independent reference proxy missing"})
            continue
        if not reference.is_file():
            raise GateError("checkpoint_ssim", f"checkpoint reference is missing: {reference}", event_sequence(event))
        reference_probe = probe(reference)
        snapshot = event.get("snapshot", {})
        empty_timeline = (
            isinstance(snapshot, dict)
            and snapshot.get("duration_frames", 0) <= 0
            and not snapshot.get("clips")
            and not snapshot.get("compositions")
            and not snapshot.get("mixes")
        )
        if empty_timeline:
            results.append({
                "sequence": event_sequence(event),
                "status": "passed",
                "accepted": True,
                "ssim": 1.0,
                "ssim_status": "not_applicable_empty_timeline",
                "reference": {"probe": reference_probe},
            })
            continue
        output = work_dir / "checkpoint-renders" / f"{event_sequence(event):08d}.mp4"
        reference_has_video = any(stream.get("codec_type") == "video" for stream in reference_probe.get("streams", []))
        proxy = event.get("reference_proxy", {})
        preset = None
        if isinstance(proxy, dict) and isinstance(proxy.get("width"), int) and isinstance(proxy.get("height"), int):
            preset = {
                "crf": "28",
                "preset": "ultrafast",
                "ab": "64k",
                "width": str(proxy["width"]),
                "height": str(proxy["height"]),
                "rescale": "bilinear",
            }
        render_event(
            session_dir,
            event,
            output,
            melt_binary=melt_binary,
            preset=preset,
            require_video=reference_has_video,
        )
        report = validate_render(reference, output, minimum_ssim=minimum_ssim)
        report["sequence"] = event_sequence(event)
        report["status"] = "passed" if report["accepted"] else "failed"
        results.append(report)
        if not report["accepted"]:
            raise GateError("checkpoint_ssim", f"checkpoint SSIM gate failed with score {report['ssim']}", event_sequence(event))
    return results


def _reference_video(session_dir: Path) -> Path:
    candidates = (
        session_dir / "output" / "final.mp4",
        session_dir / "final_ref.mp4",
        session_dir / "output" / "final.mkv",
        session_dir / "output" / "final.mov",
        session_dir / "output" / "final.webm",
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    metadata = session_dir / "internal" / "collector-metadata.json"
    if metadata.is_file():
        value = json.loads(metadata.read_text(encoding="utf-8"))
        relative = value.get("artifacts", {}).get("final_video")
        if isinstance(relative, str):
            candidate = session_dir / safe_relative(relative)
            if candidate.is_file():
                return candidate
    raise GateError("final_reference", "editor final reference render is missing")


def reference_matched_render(reference: Path) -> tuple[str, dict[str, str] | None, bool]:
    """Choose a strict comparison render that preserves lossless references."""

    media = probe(reference)
    video_streams = [stream for stream in media.get("streams", []) if stream.get("codec_type") == "video"]
    audio_streams = [stream for stream in media.get("streams", []) if stream.get("codec_type") == "audio"]
    has_video = bool(video_streams)
    if reference.suffix.lower() == ".mkv" and has_video and video_streams[0].get("codec_name") == "ffv1":
        preset = {"f": "matroska", "vcodec": "ffv1"}
        pixel_format = video_streams[0].get("pix_fmt")
        if isinstance(pixel_format, str) and pixel_format:
            preset["pix_fmt"] = pixel_format
        if audio_streams:
            preset["acodec"] = "flac" if audio_streams[0].get("codec_name") == "flac" else "pcm_s16le"
        return ".mkv", preset, has_video
    return ".mp4", None, has_video


def _clean_events(events: list[dict[str, Any]], accepted: list[dict[str, Any]]) -> list[dict[str, Any]]:
    accepted_ids = {event.get("event_id") for event in accepted}
    accepted_transactions = {event.get("transaction_id") for event in accepted if event.get("transaction_id")}
    checkpoints = [event for event in events if event.get("event_type") == "state.checkpoint"]
    accepted_hashes = {
        event.get("project_after_hash", event.get("after_hash"))
        for event in accepted
    }
    if checkpoints:
        baseline_state = checkpoints[0].get("project_state")
        accepted_hashes.add(baseline_state.get("sha256") if isinstance(baseline_state, dict) else checkpoints[0].get("state_hash"))
    checkpoint_ids = {
        checkpoint.get("event_id")
        for checkpoint in checkpoints
        if (
            checkpoint.get("project_state", {}).get("sha256")
            if isinstance(checkpoint.get("project_state"), dict)
            else checkpoint.get("state_hash")
        ) in accepted_hashes
    }
    cleaned = []
    for event in events:
        event_type = event.get("event_type")
        transaction_id = event.get("transaction_id")
        keep = (
            event.get("event_id") in accepted_ids
            or event.get("event_id") in checkpoint_ids
            or event_type in {"session.start", "session.end", "project.context", "session.recovered"}
            or (transaction_id in accepted_transactions and event_type in {"action", "ui.command", "ui.shortcut", "ui.gesture"})
        )
        if keep:
            cleaned.append(copy.deepcopy(event))
    return cleaned


def _copy_assets(session_dir: Path, bundle: Path, manifest: dict) -> None:
    for entry in manifest.get("assets", []):
        relative = safe_relative(str(entry.get("file", entry.get("path", ""))))
        source = session_dir / relative
        destination = bundle / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)


def _make_state_references_portable(
    session_dir: Path,
    trajectory: Path,
    bundle: Path,
    events: list[dict[str, Any]],
) -> None:
    for event in events:
        reference = state_reference(event)
        if isinstance(reference, dict) and reference.get("path"):
            relative = safe_relative(str(reference["path"]))
            base = session_dir if reference.get("base") == "session" else trajectory.parent
            source = base / relative
            if not source.is_file() or source.is_symlink():
                raise GateError("state_sidecars", f"project state sidecar is missing: {source}", event_sequence(event))
            suffix = "".join(source.suffixes) or ".state"
            name = f"{reference.get('sha256', sha256_file(source))}{suffix}"
            destination = bundle / "states" / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            if not destination.exists():
                shutil.copy2(source, destination)
            reference["base"] = "session"
            reference["path"] = destination.relative_to(bundle).as_posix()
        proxy = event.get("reference_proxy")
        if isinstance(proxy, dict) and proxy.get("path"):
            relative = safe_relative(str(proxy["path"]))
            base = trajectory.parent if proxy.get("base") == "trajectory" else session_dir
            source = base / relative
            if not source.is_file() or source.is_symlink():
                raise GateError("checkpoint_ssim", f"checkpoint reference is missing: {source}", event_sequence(event))
            digest = sha256_file(source)
            destination = bundle / "checkpoint_refs" / f"{digest}{source.suffix}"
            destination.parent.mkdir(parents=True, exist_ok=True)
            if not destination.exists():
                shutil.copy2(source, destination)
            proxy["base"] = "session"
            proxy["path"] = destination.relative_to(bundle).as_posix()
            proxy["sha256"] = digest
            proxy["bytes"] = source.stat().st_size


def _copy_if_present(source: Path, destination: Path) -> None:
    if source.is_file():
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)


def _public_asset_manifest(manifest: dict[str, Any]) -> dict[str, Any]:
    """Remove local source paths that are needed only during ingestion."""
    value = copy.deepcopy(manifest)
    for asset in value.get("assets", []):
        if isinstance(asset, dict):
            asset.pop("source", None)
            asset.pop("original_path", None)
    return value


def publish_bundle(
    session_dir: Path,
    destination_root: Path,
    session_id: str,
    artifacts: dict[str, Any],
    events: list[dict[str, Any]],
    accepted: list[dict[str, Any]],
    manifest: dict,
) -> Path:
    destination_root.mkdir(parents=True, exist_ok=True)
    destination = destination_root / session_id
    if destination.exists():
        raise GateError("publish", f"destination already exists: {destination}")
    temporary = Path(tempfile.mkdtemp(prefix=f".{session_id}-", dir=destination_root))
    # mkdtemp deliberately creates mode 0700. The temporary name is hidden,
    # but that mode must not survive the atomic rename or bundles produced by
    # a root-owned container cannot be inspected by the host user.
    temporary.chmod(0o755)
    try:
        shutil.copy2(artifacts["final_video"], temporary / "final.mp4")
        reference_video = artifacts.get("reference_video")
        if isinstance(reference_video, Path) and reference_video.is_file():
            _copy_if_present(reference_video, temporary / "reference" / f"editor-final{reference_video.suffix.lower()}")
        _copy_assets(session_dir, temporary, manifest)
        portable_project = remap_project_assets(
            Path(artifacts["project"]).read_bytes(),
            temporary / "reconstructed.kdenlive",
            manifest,
            temporary,
            absolute_paths=False,
        )
        (temporary / "reconstructed.kdenlive").write_bytes(portable_project)
        shutil.copy2(artifacts["report"], temporary / "render-report.json")
        cleaned_events = _clean_events(events, accepted)
        _make_state_references_portable(session_dir, artifacts["raw_trajectory"], temporary, cleaned_events)
        write_jsonl(temporary / "trajectory.jsonl", cleaned_events)
        raw_path = artifacts["raw_trajectory"]
        shutil.copy2(raw_path, temporary / "raw-trajectory.jsonl")
        write_json(temporary / "asset-manifest.json", _public_asset_manifest(manifest))
        for optional in (
            "sample.json",
            "session.json",
            "entity-map.json",
            "internal/final.kdenlive",
            "internal/collector-metadata.json",
            "internal/rationale.jsonl",
        ):
            _copy_if_present(session_dir / optional, temporary / optional)
        for raw_segment in sorted((session_dir / "evidence").glob("raw-events-*.jsonl")) if (session_dir / "evidence").is_dir() else []:
            _copy_if_present(raw_segment, temporary / "evidence" / raw_segment.name)
        bundle_manifest = {
            "schema": "video-path/bundle@1",
            "session_id": session_id,
            "published_at_utc": utc_now(),
            "files": {
                path.relative_to(temporary).as_posix(): {"sha256": sha256_file(path), "bytes": path.stat().st_size}
                for path in sorted(temporary.rglob("*"))
                if path.is_file()
            },
        }
        write_json(temporary / "bundle-manifest.json", bundle_manifest)
        os.replace(temporary, destination)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return destination


def quarantine_session(
    session_dir: Path,
    quarantine_root: Path,
    session_id: str,
    error: Exception,
    trajectory: Path | None,
) -> Path:
    quarantine_root.mkdir(parents=True, exist_ok=True)
    destination = quarantine_root / session_id
    temporary = Path(tempfile.mkdtemp(prefix=f".{session_id}-", dir=quarantine_root))
    temporary.chmod(0o755)
    try:
        if trajectory and trajectory.is_file():
            shutil.copy2(trajectory, temporary / "raw-trajectory.jsonl")
        try:
            manifest_path, manifest = load_manifest(session_dir)
            shutil.copy2(manifest_path, temporary / "asset-manifest.json")
            for entry in manifest.get("assets", []):
                relative = safe_relative(str(entry.get("file", entry.get("path", ""))))
                source = session_dir / relative
                if source.is_file() and not source.is_symlink() and source.resolve().is_relative_to(session_dir):
                    destination_asset = temporary / relative
                    destination_asset.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source, destination_asset)
        except (EditPathError, OSError, ValueError, TypeError, json.JSONDecodeError):
            # A malformed or missing manifest may be the reason this session
            # reached quarantine. The rejection record and raw JSONL must
            # still be published for triage.
            pass
        report = {
            "schema": "video-path/rejection@1",
            "session_id": session_id,
            "rejected_at_utc": utc_now(),
            "gate": error.gate if isinstance(error, GateError) else "internal",
            "sequence": error.sequence if isinstance(error, GateError) else None,
            "message": str(error),
            "source_session": str(session_dir),
        }
        write_json(temporary / "rejection.json", report)
        if destination.exists():
            destination = quarantine_root / f"{session_id}-{sha256_file(trajectory)[:12] if trajectory and trajectory.is_file() else os.getpid()}"
        os.replace(temporary, destination)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return destination


def process_session(
    session_dir: Path,
    output_root: Path,
    *,
    minimum_ssim: float = 0.995,
    minimum_commits: int = 1,
    minimum_changed_entities: int = 1,
    require_license: bool = False,
    require_complete: bool = True,
    melt_binary: str | None = None,
    runtime_lock: Path | None = None,
) -> dict[str, Any]:
    session_dir = session_dir.expanduser().resolve()
    output_root = output_root.expanduser().resolve()
    trajectory: Path | None = None
    session_id = session_dir.name
    try:
        preflight = preflight_session(
            session_dir,
            minimum_commits=minimum_commits,
            minimum_changed_entities=minimum_changed_entities,
            require_license=require_license,
            require_complete=require_complete,
        )
        trajectory = preflight["trajectory"]
        events = preflight["events"]
        envelope = preflight["envelope"]
        session_id = envelope["session_id"]
        runtime = runtime_fingerprint(melt_binary=melt_binary)
        if runtime_lock is not None:
            expected_runtime = json.loads(runtime_lock.expanduser().resolve().read_text(encoding="utf-8"))
            verify_runtime_lock(expected_runtime, runtime)
        require_targets = preflight["require_exact"]
        state_reports = preflight["state_reports"]
        project_states = preflight["project_states"]
        stable_entities = preflight["stable_entities"]
        branch = preflight["branch"]
        actions = preflight["actions"]
        activity = preflight["activity"]
        manifest_path = preflight["manifest_path"]
        manifest = preflight["manifest"]
        verified_assets = preflight["verified_assets"]
        with tempfile.TemporaryDirectory(prefix="edit-path-process-", dir=output_root if output_root.exists() else None) as temporary_value:
            work_dir = Path(temporary_value)
            checkpoint_results = validate_checkpoints(
                session_dir,
                events,
                work_dir,
                minimum_ssim=minimum_ssim,
                melt_binary=melt_binary,
                require_references=require_targets,
            )
            reference = _reference_video(session_dir)
            validation_suffix, validation_preset, reference_has_video = reference_matched_render(reference)
            if not reference_has_video:
                raise GateError("reference_render", "the editor reference render contains no video stream")
            validation_name = "reconstructed.mp4" if validation_suffix == ".mp4" and validation_preset is None else f"reconstructed-validation{validation_suffix}"
            validation_render = render_session(
                session_dir,
                work_dir / validation_name,
                melt_binary=melt_binary,
                preset=validation_preset,
            )
            project = work_dir / "reconstructed.kdenlive"
            final_report = validate_render(reference, validation_render, minimum_ssim=minimum_ssim)
            if not final_report["accepted"]:
                raise GateError("final_render", f"final render validation failed with SSIM {final_report['ssim']}")
            if validation_suffix == ".mp4" and validation_preset is None:
                reconstructed = validation_render
                delivery_report = final_report
            else:
                reconstructed = render_session(session_dir, work_dir / "reconstructed.mp4", melt_binary=melt_binary)
                delivery_report = validate_render(
                    reference,
                    reconstructed,
                    minimum_ssim=0.98,
                    maximum_duration_delta=0.10,
                )
                if not delivery_report["accepted"]:
                    raise GateError(
                        "delivery_render",
                        "MP4 delivery validation failed "
                        f"with SSIM {delivery_report['ssim']} and duration delta {delivery_report['duration_delta_seconds']}",
                    )
            report = {
                "schema": "video-path/render-report@1",
                "capture": {
                    "schema_versions": sorted({str(event.get("schema_version")) for event in events}),
                    "project_context": next((event.get("context") for event in events if event.get("event_type") == "project.context"), None),
                },
                "runtime": runtime,
                "session": envelope,
                "branch": {
                    "baseline_hash": branch.baseline_hash,
                    "final_hash": branch.final_hash,
                    "accepted_commits": len(branch.accepted),
                },
                "state_transitions": sum(event.get("event_type") == "state.diff" for event in events),
                "project_states": project_states,
                "stable_entities_checked": stable_entities,
                "action_semantics": actions,
                "semantic_activity": activity,
                "assets_verified": len(verified_assets),
                "checkpoint_results": checkpoint_results,
                "final": final_report,
                "delivery": delivery_report,
            }
            report_path = work_dir / "render-report.json"
            write_json(report_path, report)
            accepted_path = publish_bundle(
                session_dir,
                output_root / "accepted",
                session_id,
                {
                    "final_video": reconstructed,
                    "reference_video": reference,
                    "project": project,
                    "report": report_path,
                    "raw_trajectory": trajectory,
                    "manifest_path": manifest_path,
                },
                events,
                branch.accepted,
                manifest,
            )
        return {"status": "accepted", "session_id": session_id, "path": str(accepted_path)}
    except Exception as error:
        quarantine_path = quarantine_session(session_dir, output_root / "quarantine", session_id, error, trajectory)
        return {
            "status": "quarantined",
            "session_id": session_id,
            "path": str(quarantine_path),
            "gate": error.gate if isinstance(error, GateError) else "internal",
            "message": str(error),
        }


def preflight_session(
    session_dir: Path,
    *,
    minimum_commits: int = 1,
    minimum_changed_entities: int = 1,
    require_license: bool = False,
    require_complete: bool = True,
) -> dict[str, Any]:
    session_dir = session_dir.expanduser().resolve()
    trajectory = find_trajectory(session_dir)
    events = read_jsonl(trajectory)
    envelope = validate_event_envelope(events, require_complete=require_complete)
    if not any(event.get("event_type") == "state.diff" and event.get("boundary") == "commit" for event in events):
        raise GateError("semantic_activity", "session contains no committed semantic edit")
    require_exact = any(event.get("schema_version") == "0.3.0" for event in events)
    _, state_reports = validate_state_transitions(events)
    stable_entities = validate_stable_entities(events, required=require_exact)
    project_states = validate_project_state_sidecars(session_dir, trajectory, events, require_exact=require_exact)
    branch = resolve_accepted_branch(events, require_targets=require_exact)
    actions = validate_action_semantics(events, branch.accepted)
    activity = semantic_activity(
        branch.accepted,
        minimum_commits=minimum_commits,
        minimum_changed_entities=minimum_changed_entities,
    )
    manifest_path, manifest = load_manifest(session_dir)
    verified_assets = verify_assets(session_dir, manifest, require_approved_license=require_license)
    return {
        "trajectory": trajectory,
        "events": events,
        "envelope": envelope,
        "require_exact": require_exact,
        "state_reports": state_reports,
        "stable_entities": stable_entities,
        "project_states": project_states,
        "branch": branch,
        "actions": actions,
        "activity": activity,
        "manifest_path": manifest_path,
        "manifest": manifest,
        "verified_assets": verified_assets,
    }


def _reject_symlinks(root: Path) -> None:
    if root.is_symlink():
        raise GateError("ingestion", "session root may not be a symlink")
    for path in root.rglob("*"):
        if path.is_symlink():
            raise GateError("ingestion", f"session contains a symlink: {path.relative_to(root)}")


def ingest_session(session_dir: Path, queue_root: Path) -> dict[str, Any]:
    session_dir = session_dir.expanduser().absolute()
    queue_root = queue_root.expanduser().resolve()
    trajectory: Path | None = None
    session_id = session_dir.name
    temporary: Path | None = None
    try:
        _reject_symlinks(session_dir)
        preflight = preflight_session(session_dir)
        trajectory = preflight["trajectory"]
        session_id = preflight["envelope"]["session_id"]
        if Path(session_id).name != session_id or session_id in {".", ".."}:
            raise GateError("ingestion", "session_id is not a safe queue directory name")
        queued_root = queue_root / "queued"
        queued_root.mkdir(parents=True, exist_ok=True)
        destination = queued_root / session_id
        if destination.exists():
            raise GateError("ingestion", f"session is already queued: {session_id}")
        temporary = Path(tempfile.mkdtemp(prefix=f".{session_id}-", dir=queued_root))
        temporary.chmod(0o755)
        shutil.copytree(session_dir, temporary, dirs_exist_ok=True)
        preflight_session(temporary)
        os.replace(temporary, destination)
        temporary = None
        return {"status": "queued", "session_id": session_id, "path": str(destination)}
    except Exception as error:
        if temporary is not None:
            shutil.rmtree(temporary, ignore_errors=True)
        quarantine = quarantine_session(session_dir, queue_root / "quarantine", session_id, error, trajectory)
        return {
            "status": "quarantined",
            "session_id": session_id,
            "path": str(quarantine),
            "gate": error.gate if isinstance(error, GateError) else "internal",
            "message": str(error),
        }


def process_next_queued(
    queue_root: Path,
    output_root: Path,
    **process_options: Any,
) -> dict[str, Any]:
    queue_root = queue_root.expanduser().resolve()
    queued_root = queue_root / "queued"
    candidates = sorted(path for path in queued_root.glob("*") if path.is_dir()) if queued_root.is_dir() else []
    if not candidates:
        return {"status": "idle"}
    source = candidates[0]
    processing_root = queue_root / "processing"
    processing_root.mkdir(parents=True, exist_ok=True)
    claimed = processing_root / f"{source.name}-{os.getpid()}"
    os.replace(source, claimed)
    result = process_session(claimed, output_root, **process_options)
    archive_root = queue_root / ("completed" if result["status"] == "accepted" else "rejected")
    archive_root.mkdir(parents=True, exist_ok=True)
    archived = archive_root / source.name
    if archived.exists():
        archived = archive_root / f"{source.name}-{os.getpid()}"
    os.replace(claimed, archived)
    result["source_archive"] = str(archived)
    return result


def build_qa_queue(output_root: Path, *, sample_rate: float = 0.1, seed: str = "video-path-qa-v1") -> dict[str, Any]:
    if not 0 < sample_rate <= 1:
        raise ValueError("QA sample rate must be in (0, 1]")
    output_root = output_root.expanduser().resolve()
    bundles = sorted(path for path in (output_root / "accepted").glob("*") if path.is_dir()) if (output_root / "accepted").is_dir() else []
    selected = []
    for bundle in bundles:
        value = int(hashlib.sha256(f"{seed}:{bundle.name}".encode()).hexdigest(), 16) / (2**256 - 1)
        if value <= sample_rate:
            selected.append({
                "session_id": bundle.name,
                "video": (bundle / "final.mp4").relative_to(output_root).as_posix(),
                "trajectory": (bundle / "trajectory.jsonl").relative_to(output_root).as_posix(),
                "review": f"qa-reviews/{bundle.name}.json",
            })
    if bundles and not selected:
        bundle = bundles[0]
        selected.append({
            "session_id": bundle.name,
            "video": (bundle / "final.mp4").relative_to(output_root).as_posix(),
            "trajectory": (bundle / "trajectory.jsonl").relative_to(output_root).as_posix(),
            "review": f"qa-reviews/{bundle.name}.json",
        })
    queue = {"schema": "video-path/qa-queue@1", "generated_at_utc": utc_now(), "sample_rate": sample_rate, "seed": seed, "samples": selected}
    write_json(output_root / "qa-review-queue.json", queue)
    return queue


def record_qa_review(output_root: Path, session_id: str, *, reviewer: str, status: str, notes: str) -> dict[str, Any]:
    if status not in {"passed", "rejected"}:
        raise ValueError("QA status must be passed or rejected")
    if Path(session_id).name != session_id:
        raise ValueError("QA session_id must be one safe path component")
    output_root = output_root.expanduser().resolve()
    bundle = output_root / "accepted" / safe_relative(session_id)
    if not bundle.is_dir():
        raise ValueError(f"accepted bundle does not exist: {session_id}")
    review = {"schema": "video-path/qa-review@1", "session_id": session_id, "reviewed_at_utc": utc_now(), "reviewer": reviewer, "status": status, "notes": notes}
    write_json(output_root / "qa-reviews" / f"{session_id}.json", review)
    return review


def build_dataset_index(output_root: Path) -> dict[str, Any]:
    output_root = output_root.expanduser().resolve()
    samples = []
    excluded = []
    for bundle in sorted((output_root / "accepted").glob("*")) if (output_root / "accepted").is_dir() else []:
        manifest = bundle / "bundle-manifest.json"
        report = bundle / "render-report.json"
        if not manifest.is_file() or not report.is_file():
            continue
        manifest_value = json.loads(manifest.read_text(encoding="utf-8"))
        report_value = json.loads(report.read_text(encoding="utf-8"))
        review_path = output_root / "qa-reviews" / f"{bundle.name}.json"
        review = json.loads(review_path.read_text(encoding="utf-8")) if review_path.is_file() else None
        if review and review.get("status") == "rejected":
            excluded.append({"session_id": bundle.name, "reason": "human_qa_rejected", "review": review_path.relative_to(output_root).as_posix()})
            continue
        samples.append({
            "session_id": manifest_value.get("session_id"),
            "bundle": bundle.relative_to(output_root).as_posix(),
            "bundle_manifest_sha256": sha256_file(manifest),
            "schema": report_value.get("schema"),
            "capture_schema_versions": report_value.get("capture", {}).get("schema_versions", []),
            "kdenlive_build": (report_value.get("capture", {}).get("project_context") or {}).get("kdenlive_build"),
            "mlt_version": (report_value.get("capture", {}).get("project_context") or {}).get("mlt_version"),
            "ssim": report_value.get("final", {}).get("ssim"),
            "accepted_commits": report_value.get("branch", {}).get("accepted_commits"),
            "qa_status": review.get("status") if review else "not_reviewed",
        })
    index = {"schema": "video-path/dataset-index@1", "generated_at_utc": utc_now(), "samples": samples, "excluded": excluded}
    write_json(output_root / "dataset-index.json", index)
    return index
