#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Video Path Pilot contributors
# SPDX-License-Identifier: GPL-3.0-only
"""Validate the MVP sample JSON and its referenced local artifacts."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def validate_sample(path: Path, check_files: bool = False) -> list[str]:
    errors: list[str] = []
    try:
        sample = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return [f"cannot read sample: {exc}"]
    for key in ("schema_version", "sample_id", "task", "project", "inputs", "edit_path", "output", "quality", "evidence", "provenance"):
        if key not in sample: errors.append(f"missing top-level field: {key}")
    if "rationale" in sample: errors.append("editor intent is prohibited: remove rationale")
    if sample.get("schema_version") != "0.1.0": errors.append("unsupported schema_version")
    task = sample.get("task", {})
    if "editor_plan" in task: errors.append("editor intent is prohibited: remove task.editor_plan")
    prompt = task.get("prompt")
    if prompt is None:
        if task.get("prompt_status") != "pending_internal_entry": errors.append("missing prompt must be marked pending_internal_entry")
    elif not isinstance(prompt, str) or not prompt.strip():
        errors.append("task.prompt must be non-empty or explicitly pending")
    rate = sample.get("project", {}).get("frame_rate", {})
    if not isinstance(rate.get("numerator"), int) or rate.get("numerator", 0) <= 0: errors.append("invalid frame-rate numerator")
    if not isinstance(rate.get("denominator"), int) or rate.get("denominator", 0) <= 0: errors.append("invalid frame-rate denominator")
    assets = sample.get("inputs", {}).get("assets", [])
    embedded_assets = sample.get("inputs", {}).get("embedded_assets", [])
    if not isinstance(assets, list): errors.append("inputs.assets must be a list"); assets = []
    if not isinstance(embedded_assets, list): errors.append("inputs.embedded_assets must be a list"); embedded_assets = []
    if not assets and not embedded_assets: errors.append("sample requires at least one file-backed or embedded input asset")
    ids = [a.get("asset_id") for a in assets + embedded_assets if isinstance(a, dict)]
    if len(ids) != len(set(ids)): errors.append("asset IDs must be unique")
    for index, asset in enumerate(embedded_assets):
        if not isinstance(asset, dict) or not asset.get("asset_id") or not asset.get("native_reference") or not asset.get("service"):
            errors.append(f"embedded asset {index + 1} is incomplete")
    operations = sample.get("edit_path", {}).get("operations", [])
    if not operations: errors.append("edit_path requires at least one accepted operation")
    for index, operation in enumerate(operations):
        if isinstance(operation, dict) and "rationale_note_ids" in operation:
            errors.append(f"editor intent is prohibited: remove operation {index + 1} rationale_note_ids")
    if sample.get("quality", {}).get("unresolved_asset_ids"): errors.append("sample has unresolved asset bindings")
    if sample.get("quality", {}).get("output_completion_confirmed") is not True: errors.append("output completion must be confirmed")
    if check_files:
        root = path.parent
        references = [(a.get("file"), a.get("sha256")) for a in assets if isinstance(a, dict)]
        references += [
            (sample.get("output", {}).get("video"), sample.get("output", {}).get("sha256")),
            (sample.get("evidence", {}).get("native_project"), sample.get("evidence", {}).get("native_project_sha256")),
        ]
        if sample.get("output", {}).get("reconstructed_video"):
            references.append((sample["output"]["reconstructed_video"], sample["output"].get("reconstructed_video_sha256")))
        if sample.get("output", {}).get("edit_process_video"):
            references.append((sample["output"]["edit_process_video"], sample["output"].get("edit_process_video_sha256")))
        for raw in sample.get("evidence", {}).get("raw_events", []):
            if isinstance(raw, dict): references.append((raw.get("file"), raw.get("sha256")))
        for relative, expected in references:
            if not isinstance(relative, str): errors.append("artifact path is missing"); continue
            artifact = root / relative
            if not artifact.is_file(): errors.append(f"artifact is missing: {relative}")
            elif expected and digest(artifact) != expected: errors.append(f"artifact hash mismatch: {relative}")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("sample", type=Path)
    parser.add_argument("--check-files", action="store_true")
    args = parser.parse_args()
    errors = validate_sample(args.sample, args.check_files)
    if errors:
        for error in errors: print(error, file=sys.stderr)
        return 1
    print(f"valid sample: {args.sample}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
