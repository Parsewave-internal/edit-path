# SPDX-FileCopyrightText: 2026 Edit Path contributors
# SPDX-License-Identifier: GPL-3.0-only

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

from .errors import EditPathError
from .io import find_trajectory, read_jsonl
from .pipeline import (
    build_qa_queue,
    build_dataset_index,
    ingest_session,
    attribution_coverage,
    process_next_queued,
    process_session,
    record_qa_review,
    semantic_activity,
    validate_event_envelope,
)
from .reconstruct import materialize_project, render_session, select_video_encoder
from .runtime import write_runtime_lock
from .state import resolve_accepted_branch, validate_action_semantics, validate_state_transitions
from .validate import validate_render


def session_path(value: str) -> Path:
    return Path(value).expanduser().resolve()


def command_inspect(args: argparse.Namespace) -> int:
    session = session_path(args.session)
    events = read_jsonl(find_trajectory(session))
    envelope = validate_event_envelope(events, require_complete=False)
    result: dict = {"session": envelope}
    try:
        _, states = validate_state_transitions(events)
        branch = resolve_accepted_branch(events, require_targets=any(event.get("schema_version") == "0.3.0" for event in events))
        actions = validate_action_semantics(events, branch.accepted)
        result.update({
            "state_transitions_valid": True,
            "state_events": len(states),
            "accepted_commits": len(branch.accepted),
            "baseline_hash": branch.baseline_hash,
            "final_hash": branch.final_hash,
            "semantic_activity": semantic_activity(branch.accepted),
            "action_semantics": actions,
            "attribution_coverage": attribution_coverage(actions, events),
        })
    except EditPathError as error:
        result.update({"state_transitions_valid": False, "state_error": str(error)})
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result.get("state_transitions_valid") else 2


def command_reconstruct(args: argparse.Namespace) -> int:
    session = session_path(args.session)
    output = Path(args.output).expanduser().resolve() if args.output else None
    result = materialize_project(session, output) if args.project_only else render_session(session, output, melt_binary=args.melt)
    print(result)
    return 0


def command_validate(args: argparse.Namespace) -> int:
    report = validate_render(
        Path(args.reference),
        Path(args.reconstructed),
        Path(args.report) if args.report else None,
        minimum_ssim=args.minimum_ssim,
    )
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["accepted"] else 2


def command_process(args: argparse.Namespace) -> int:
    result = process_session(
        session_path(args.session),
        session_path(args.output_root),
        minimum_ssim=args.minimum_ssim,
        minimum_final_ssim=args.minimum_final_ssim,
        minimum_commits=args.minimum_commits,
        minimum_changed_entities=args.minimum_changed_entities,
        require_license=args.require_license,
        require_complete=not args.allow_partial,
        melt_binary=args.melt,
        runtime_lock=Path(args.runtime_lock) if args.runtime_lock else None,
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["status"] == "accepted" else 2


def command_index(args: argparse.Namespace) -> int:
    result = build_dataset_index(session_path(args.output_root))
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


def command_ingest(args: argparse.Namespace) -> int:
    result = ingest_session(session_path(args.session), session_path(args.queue_root))
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["status"] == "queued" else 2


def command_work_one(args: argparse.Namespace) -> int:
    result = process_next_queued(
        session_path(args.queue_root),
        session_path(args.output_root),
        minimum_ssim=args.minimum_ssim,
        minimum_final_ssim=args.minimum_final_ssim,
        minimum_commits=args.minimum_commits,
        minimum_changed_entities=args.minimum_changed_entities,
        require_license=args.require_license,
        melt_binary=args.melt,
        runtime_lock=Path(args.runtime_lock) if args.runtime_lock else None,
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["status"] in {"accepted", "idle"} else 2


def command_qa_sample(args: argparse.Namespace) -> int:
    result = build_qa_queue(session_path(args.output_root), sample_rate=args.sample_rate, seed=args.seed)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


def command_qa_review(args: argparse.Namespace) -> int:
    result = record_qa_review(
        session_path(args.output_root),
        args.session_id,
        reviewer=args.reviewer,
        status=args.status,
        notes=args.notes,
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


def command_doctor(_args: argparse.Namespace) -> int:
    tools = {name: shutil.which(name) for name in ("melt", "mlt-melt", "ffmpeg", "ffprobe")}
    try:
        video_encoder = select_video_encoder(tools["ffmpeg"])
        encoder_error = None
    except EditPathError as error:
        video_encoder = None
        encoder_error = str(error)
    try:
        import zstandard

        tools["zstandard"] = zstandard.__version__
    except ImportError:
        tools["zstandard"] = None
    tools["python"] = sys.executable
    tools["video_encoder"] = video_encoder
    if encoder_error:
        tools["video_encoder_error"] = encoder_error
    print(json.dumps(tools, indent=2, sort_keys=True))
    return 0 if (tools["melt"] or tools["mlt-melt"]) and tools["ffmpeg"] and tools["ffprobe"] and tools["zstandard"] and video_encoder else 1


def command_lock_runtime(args: argparse.Namespace) -> int:
    value = write_runtime_lock(Path(args.output).expanduser().resolve(), melt_binary=args.melt)
    print(json.dumps(value, indent=2, sort_keys=True))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="edit-path", description="Video Path reconstruction and dataset pipeline")
    commands = parser.add_subparsers(dest="command", required=True)

    inspect = commands.add_parser("inspect", help="validate and summarize a raw session")
    inspect.add_argument("session")
    inspect.set_defaults(function=command_inspect)

    reconstruct = commands.add_parser("reconstruct", help="materialize a project or render an MP4")
    reconstruct.add_argument("session")
    reconstruct.add_argument("--output")
    reconstruct.add_argument("--project-only", action="store_true")
    reconstruct.add_argument("--melt")
    reconstruct.set_defaults(function=command_reconstruct)

    validate = commands.add_parser("validate", help="compare a reconstruction with an independent reference")
    validate.add_argument("reference")
    validate.add_argument("reconstructed")
    validate.add_argument("--report")
    validate.add_argument("--minimum-ssim", type=float, default=0.995)
    validate.set_defaults(function=command_validate)

    process = commands.add_parser("process", help="run all gates and publish to accepted or quarantine")
    process.add_argument("session")
    process.add_argument("output_root")
    process.add_argument("--minimum-ssim", type=float, default=0.995)
    process.add_argument("--minimum-final-ssim", type=float, default=0.99)
    process.add_argument("--minimum-commits", type=int, default=1)
    process.add_argument("--minimum-changed-entities", type=int, default=1)
    process.add_argument("--require-license", action="store_true", help="optional future publication gate")
    process.add_argument("--allow-partial", action="store_true", help="diagnostic only; accepted publication still requires other gates")
    process.add_argument("--melt")
    process.add_argument("--runtime-lock", help="enforce an exact melt/FFmpeg/container runtime lock")
    process.set_defaults(function=command_process)

    index = commands.add_parser("index", help="rebuild the accepted dataset index")
    index.add_argument("output_root")
    index.set_defaults(function=command_index)

    ingest = commands.add_parser("ingest", help="validate and atomically enqueue a raw session")
    ingest.add_argument("session")
    ingest.add_argument("queue_root")
    ingest.set_defaults(function=command_ingest)

    worker = commands.add_parser("work-one", help="atomically claim and process one queued session")
    worker.add_argument("queue_root")
    worker.add_argument("output_root")
    worker.add_argument("--minimum-ssim", type=float, default=0.995)
    worker.add_argument("--minimum-final-ssim", type=float, default=0.99)
    worker.add_argument("--minimum-commits", type=int, default=1)
    worker.add_argument("--minimum-changed-entities", type=int, default=1)
    worker.add_argument("--require-license", action="store_true")
    worker.add_argument("--melt")
    worker.add_argument("--runtime-lock")
    worker.set_defaults(function=command_work_one)

    qa_sample = commands.add_parser("qa-sample", help="build a deterministic human spot-check queue")
    qa_sample.add_argument("output_root")
    qa_sample.add_argument("--sample-rate", type=float, default=0.1)
    qa_sample.add_argument("--seed", default="video-path-qa-v1")
    qa_sample.set_defaults(function=command_qa_sample)

    qa_review = commands.add_parser("qa-review", help="record a human QA decision outside the immutable bundle")
    qa_review.add_argument("output_root")
    qa_review.add_argument("session_id")
    qa_review.add_argument("--reviewer", required=True)
    qa_review.add_argument("--status", choices=("passed", "rejected"), required=True)
    qa_review.add_argument("--notes", required=True)
    qa_review.set_defaults(function=command_qa_review)

    doctor = commands.add_parser("doctor", help="check reconstruction dependencies")
    doctor.set_defaults(function=command_doctor)

    lock = commands.add_parser("lock-runtime", help="write the current renderer versions as a production lock")
    lock.add_argument("output")
    lock.add_argument("--melt")
    lock.set_defaults(function=command_lock_runtime)
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        args = build_parser().parse_args(argv)
        return args.function(args)
    except (EditPathError, OSError, ValueError, json.JSONDecodeError) as error:
        print(f"edit-path: {error}", file=sys.stderr)
        return 1
