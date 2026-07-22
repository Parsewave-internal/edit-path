# SPDX-FileCopyrightText: 2026 Edit Path contributors
# SPDX-License-Identifier: GPL-3.0-only

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any, Iterable

from .errors import EditPathError


RAW_SCHEMA_VERSIONS = {"0.1.0", "0.2.0", "0.3.0"}
TRAJECTORY_SCHEMA = "edit-path/trajectory@1"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def write_jsonl(path: Path, events: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    with temporary.open("w", encoding="utf-8") as stream:
        for event in events:
            stream.write(json.dumps(event, ensure_ascii=False, separators=(",", ":"), sort_keys=True))
            stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def read_jsonl(path: Path, *, max_line_bytes: int = 64 * 1024 * 1024) -> list[dict[str, Any]]:
    if not path.is_file():
        raise EditPathError(f"trajectory not found: {path}")
    events: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            if len(line.encode("utf-8")) > max_line_bytes:
                raise EditPathError(f"trajectory line {line_number} exceeds the size limit")
            try:
                event = json.loads(line)
            except json.JSONDecodeError as exc:
                raise EditPathError(f"invalid JSON on trajectory line {line_number}: {exc}") from exc
            if not isinstance(event, dict):
                raise EditPathError(f"trajectory line {line_number} is not an object")
            events.append(event)
    if not events:
        raise EditPathError("trajectory has no events")
    return events


def event_sequence(event: dict[str, Any]) -> int | None:
    value = event.get("sequence", event.get("seq"))
    return value if isinstance(value, int) else None


def find_trajectory(session_dir: Path) -> Path:
    if session_dir.is_file() and session_dir.suffix == ".jsonl":
        return session_dir
    candidates = (
        session_dir / "evidence" / "raw-events.jsonl",
        session_dir / "trajectory.jsonl",
        session_dir / "raw-events.jsonl",
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise EditPathError(f"no trajectory JSONL found under {session_dir}")


def safe_relative(path: str) -> Path:
    value = Path(path)
    if not value.parts or value.is_absolute() or ".." in value.parts:
        raise EditPathError(f"unsafe bundle path: {path!r}")
    return value
