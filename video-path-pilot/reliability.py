#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only
"""Crash diagnostics and independent project recovery for EditPath sessions."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import tempfile
import xml.etree.ElementTree as ET
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SENSITIVE_PATH = re.compile(r"(?i)(?:[A-Z]:\\Users\\[^\\\r\n]+|/home/[^/\r\n]+)")
DECODER_PATTERNS = {
    "invalid_nal_unit_size": re.compile(r"Invalid NAL unit size", re.I),
    "missing_picture": re.compile(r"missing picture in access unit", re.I),
    "bad_source_image": re.compile(r"bad src image pointers", re.I),
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def valid_project(path: Path) -> tuple[bool, str]:
    if not path.is_file() or path.stat().st_size == 0:
        return False, "missing or empty"
    try:
        root = ET.parse(path).getroot()
    except (ET.ParseError, OSError) as error:
        return False, str(error)
    return (root.tag in {"mlt", "kdenlive"}), f"unexpected root element {root.tag!r}"


def _read_manifest(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"schema_version": "1.0", "snapshots": []}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {"schema_version": "1.0", "snapshots": []}
    except (OSError, json.JSONDecodeError):
        return {"schema_version": "1.0", "snapshots": []}


def _atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as output:
        json.dump(value, output, indent=2, ensure_ascii=False)
        output.write("\n")
        temporary = Path(output.name)
    os.replace(temporary, path)


def create_snapshot(session: Path, reason: str = "periodic", keep: int = 10) -> dict[str, Any]:
    project = session / "edit.kdenlive"
    valid, detail = valid_project(project)
    if not valid:
        raise ValueError(f"project snapshot rejected: {detail}")
    recovery = session / "recovery"
    recovery.mkdir(parents=True, exist_ok=True)
    manifest_path = recovery / "manifest.json"
    manifest = _read_manifest(manifest_path)
    snapshots = manifest.setdefault("snapshots", [])
    digest = sha256(project)
    if snapshots and snapshots[-1].get("sha256") == digest and reason == "periodic":
        return {"created": False, "reason": "unchanged", "snapshot": snapshots[-1]}

    sequence = max((int(item.get("sequence", 0)) for item in snapshots), default=0) + 1
    name = f"project-{sequence:06d}.kdenlive"
    target = recovery / name
    temporary = recovery / f".{name}.tmp"
    shutil.copy2(project, temporary)
    copied_valid, copied_detail = valid_project(temporary)
    if not copied_valid or sha256(temporary) != digest:
        temporary.unlink(missing_ok=True)
        raise ValueError(f"copied project snapshot rejected: {copied_detail}")
    os.replace(temporary, target)
    entry = {
        "sequence": sequence,
        "timestamp_utc": utc_now(),
        "file": name,
        "sha256": digest,
        "bytes": target.stat().st_size,
        "reason": reason,
        "validation": "valid_xml",
    }
    snapshots.append(entry)

    milestones = {"pre_render", "pre_finish", "crash_recovery"}
    periodic = [item for item in snapshots if item.get("reason") not in milestones]
    remove = periodic[:-max(1, keep)]
    remove_names = {item.get("file") for item in remove}
    for name_to_remove in remove_names:
        if isinstance(name_to_remove, str):
            (recovery / name_to_remove).unlink(missing_ok=True)
    manifest["snapshots"] = [item for item in snapshots if item.get("file") not in remove_names]
    manifest["updated_at_utc"] = utc_now()
    _atomic_json(manifest_path, manifest)
    return {"created": True, "snapshot": entry}


def select_recovery(session: Path) -> dict[str, Any]:
    candidates: list[dict[str, Any]] = []
    recovery = session / "recovery"
    manifest = _read_manifest(recovery / "manifest.json")
    paths = [session / "edit.kdenlive", *session.glob("edit_backup*.kdenlive")]
    paths.extend(recovery / str(item["file"]) for item in manifest.get("snapshots", []) if item.get("file"))
    for path in paths:
        valid, detail = valid_project(path)
        if path.exists():
            candidates.append(
                {
                    "path": str(path),
                    "valid": valid,
                    "validation": detail if not valid else "valid_xml",
                    "mtime_ns": path.stat().st_mtime_ns,
                    "bytes": path.stat().st_size,
                    "sha256": sha256(path) if valid else None,
                }
            )
    valid_candidates = [item for item in candidates if item["valid"]]
    selected = max(valid_candidates, key=lambda item: item["mtime_ns"]) if valid_candidates else None
    return {"selected": selected, "candidates": sorted(candidates, key=lambda item: item["mtime_ns"], reverse=True)}


def restore_recovery(session: Path) -> dict[str, Any]:
    report = select_recovery(session)
    selected = report["selected"]
    if not selected:
        raise ValueError("no valid recovery candidate exists")
    source = Path(selected["path"])
    project = session / "edit.kdenlive"
    if source == project:
        return {"restored": False, "reason": "main_project_is_newest", "selected": selected}
    recovery = session / "recovery"
    recovery.mkdir(parents=True, exist_ok=True)
    if project.exists():
        preserved = recovery / f"pre-restore-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.kdenlive"
        shutil.copy2(project, preserved)
    temporary = session / ".edit.kdenlive.restore"
    shutil.copy2(source, temporary)
    valid, detail = valid_project(temporary)
    if not valid or sha256(temporary) != selected["sha256"]:
        temporary.unlink(missing_ok=True)
        raise ValueError(f"recovery restore verification failed: {detail}")
    os.replace(temporary, project)
    return {"restored": True, "selected": selected, "destination": str(project)}


def sanitize(text: str) -> str:
    return SENSITIVE_PATH.sub("<USER_HOME>", text)


def windows_evidence() -> dict[str, Any]:
    if os.name != "nt":
        return {"available": False, "reason": "not_windows"}
    powershell = shutil.which("powershell.exe") or shutil.which("powershell")
    if not powershell:
        return {"available": False, "reason": "powershell_unavailable"}
    commands = {
        "hardware": (
            "Get-CimInstance Win32_OperatingSystem,Win32_ComputerSystem,Win32_VideoController "
            "| Select-Object __CLASS,Caption,Version,OSArchitecture,TotalPhysicalMemory,Name,DriverVersion "
            "| ConvertTo-Json -Depth 3"
        ),
        "application_errors": (
            "$start=(Get-Date).AddDays(-2); "
            "Get-WinEvent -FilterHashtable @{LogName='Application';StartTime=$start} -ErrorAction SilentlyContinue "
            "| Where-Object {$_.ProviderName -in @('Application Error','Windows Error Reporting') "
            "-and $_.Message -match 'kdenlive|EditPath'} "
            "| Select-Object -First 20 TimeCreated,Id,ProviderName,LevelDisplayName,Message "
            "| ConvertTo-Json -Depth 3"
        ),
    }
    evidence: dict[str, Any] = {"available": True}
    for name, command in commands.items():
        try:
            completed = subprocess.run(
                [powershell, "-NoProfile", "-NonInteractive", "-Command", command],
                capture_output=True,
                text=True,
                timeout=20,
            )
            evidence[name] = {
                "exit_code": completed.returncode,
                "output": sanitize(completed.stdout[-30000:]),
                "error": sanitize(completed.stderr[-4000:]),
            }
        except (OSError, subprocess.TimeoutExpired) as error:
            evidence[name] = {"error": str(error)}
    return evidence


def _event_summary(session: Path) -> dict[str, Any]:
    files = sorted(session.glob("raw-events-*.jsonl"))
    total = 0
    ended = False
    malformed = 0
    for path in files:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                event = json.loads(line)
                total += 1
                ended = event.get("event_type") == "session.end"
            except json.JSONDecodeError:
                malformed += 1
    return {"segments": len(files), "events": total, "malformed_lines": malformed, "session_end_recorded": ended}


def _decoder_counts(session: Path) -> dict[str, int]:
    combined = "\n".join(path.read_text(encoding="utf-8", errors="replace") for path in session.glob("kdenlive-console-*.log"))
    return {name: len(pattern.findall(combined)) for name, pattern in DECODER_PATTERNS.items()}


def project_media(session: Path) -> list[Path]:
    project = session / "edit.kdenlive"
    valid, _ = valid_project(project)
    if not valid:
        return []
    root = ET.parse(project).getroot()
    project_root = Path(root.get("root") or project.parent)
    found: list[Path] = []
    for property_element in root.findall(".//property[@name='resource']"):
        value = (property_element.text or "").strip()
        if not value or value in {"black", "blue"} or value.startswith(("#", "color:")):
            continue
        if re.match(r"^[0-9.]+:", value) and not re.match(r"^[A-Za-z]:[\\/]", value):
            value = value.split(":", 1)[1]
        path = Path(value)
        if not path.is_absolute():
            path = project_root / path
        try:
            path = path.resolve()
        except OSError:
            pass
        if path not in found:
            found.append(path)
    return found


def media_preflight(session: Path) -> list[dict[str, Any]]:
    ffprobe = shutil.which("ffprobe")
    ffmpeg = shutil.which("ffmpeg")
    results: list[dict[str, Any]] = []
    for index, path in enumerate(project_media(session), 1):
        item: dict[str, Any] = {
            "asset_id": f"asset-{index:03d}",
            "filename": path.name,
            "exists": path.is_file(),
            "bytes": path.stat().st_size if path.is_file() else None,
            "sha256": sha256(path) if path.is_file() else None,
        }
        if path.is_file() and ffprobe:
            probe = subprocess.run(
                [ffprobe, "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
                capture_output=True,
                text=True,
                timeout=30,
            )
            item["ffprobe_passed"] = probe.returncode == 0
            item["ffprobe"] = json.loads(probe.stdout) if probe.returncode == 0 else {"error": sanitize(probe.stderr[-4000:])}
        if path.is_file() and ffmpeg:
            try:
                decode = subprocess.run(
                    [ffmpeg, "-v", "error", "-i", str(path), "-map", "0:v:0?", "-map", "0:a:0?", "-f", "null", "-"],
                    capture_output=True,
                    text=True,
                    timeout=120,
                )
                item["decode_passed"] = decode.returncode == 0
                item["decode_errors"] = sanitize(decode.stderr[-8000:])
            except subprocess.TimeoutExpired:
                item["decode_passed"] = False
                item["decode_errors"] = "decode validation timed out after 120 seconds"
        results.append(item)
    return results


def classify(session: Path) -> dict[str, Any]:
    try:
        manifest = json.loads((session / "session.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        manifest = {}
    events = _event_summary(session)
    decoder = _decoder_counts(session)
    status = str(manifest.get("status", "unknown"))
    crashed = bool(manifest.get("last_exit_crashed"))
    if status == "start_failed":
        kind = "startup_failure"
    elif crashed:
        kind = "process_crash"
    elif any(decoder.values()):
        kind = "media_decode_error"
    elif not events["session_end_recorded"]:
        kind = "missing_session_end"
    elif status == "validation_failed":
        kind = "recording_validation_failure"
    else:
        kind = "none"
    return {
        "schema_version": "1.0",
        "generated_at_utc": utc_now(),
        "failure_type": kind,
        "process": "kdenlive",
        "exit_code": manifest.get("last_exit_code"),
        "exit_status": manifest.get("last_exit_status", "unknown"),
        "process_error": manifest.get("last_process_error", ""),
        "events": events,
        "decoder_errors": decoder,
        "media_preflight": media_preflight(session),
        "recovery": select_recovery(session),
    }


def export_diagnostics(session: Path, destination: Path | None = None) -> Path:
    destination = destination or session.parent / f"EditPath-Diagnostics-{session.name}.zip"
    diagnosis = classify(session)
    allow_names = {
        "session.json",
        "supervisor-activity.log",
        "recovery/manifest.json",
    }
    files = [
        path
        for path in session.rglob("*")
        if path.is_file()
        and (
            path.relative_to(session).as_posix() in allow_names
            or path.name.startswith(("kdenlive-console-", "raw-events-"))
            or path.suffix.lower() in {".dmp"}
        )
    ]
    system = {
        "generated_at_utc": utc_now(),
        "platform": platform.platform(),
        "python": platform.python_version(),
        "machine": platform.machine(),
        "package_commit": os.environ.get("EDIT_PATH_BUILD_COMMIT", "unknown"),
        "windows": windows_evidence(),
    }
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("diagnosis.json", sanitize(json.dumps(diagnosis, indent=2)))
        archive.writestr("system.json", sanitize(json.dumps(system, indent=2)))
        for path in files:
            relative = path.relative_to(session).as_posix()
            if path.suffix.lower() == ".dmp":
                archive.write(path, f"session/{relative}")
            else:
                archive.writestr(f"session/{relative}", sanitize(path.read_text(encoding="utf-8", errors="replace")))
    return destination


def main() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    snapshot = subparsers.add_parser("snapshot")
    snapshot.add_argument("session", type=Path)
    snapshot.add_argument("--reason", default="periodic")
    snapshot.add_argument("--keep", type=int, default=10)
    recovery = subparsers.add_parser("select-recovery")
    recovery.add_argument("session", type=Path)
    restore = subparsers.add_parser("restore-recovery")
    restore.add_argument("session", type=Path)
    diagnostics = subparsers.add_parser("diagnostics")
    diagnostics.add_argument("session", type=Path)
    diagnostics.add_argument("--output", type=Path)
    arguments = parser.parse_args()
    if arguments.command == "snapshot":
        result: Any = create_snapshot(arguments.session, arguments.reason, arguments.keep)
    elif arguments.command == "select-recovery":
        result = select_recovery(arguments.session)
    elif arguments.command == "restore-recovery":
        result = restore_recovery(arguments.session)
    else:
        result = {"diagnostics": str(export_diagnostics(arguments.session, arguments.output))}
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
