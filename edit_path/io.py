# SPDX-FileCopyrightText: 2026 Edit Path contributors
# SPDX-License-Identifier: GPL-3.0-only

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any, Iterable, Sequence

from .errors import EditPathError


RAW_SCHEMA_VERSIONS = {"0.1.0", "0.2.0", "0.3.0"}
TRAJECTORY_SCHEMA = "edit-path/trajectory@1"
REPLACE_RETRY_DELAYS = (0.05, 0.10, 0.25, 0.50, 1.00, 1.00, 1.00, 1.00)


def replace_with_retry(
    source: Path | str,
    destination: Path | str,
    *,
    retry_delays: Sequence[float] = REPLACE_RETRY_DELAYS,
) -> None:
    """Atomically replace a path, tolerating short-lived file scanner locks.

    Windows antivirus and indexing services can briefly hold a newly written
    file or a child of a newly populated directory. During that window both
    ``os.replace`` and ``os.rename`` report access denied even though the
    caller owns the paths. Retrying only permission/sharing failures preserves
    the atomic publication contract without hiding permanent or unrelated
    filesystem errors.
    """

    for attempt in range(len(retry_delays) + 1):
        try:
            os.replace(source, destination)
            return
        except OSError as error:
            retryable = isinstance(error, PermissionError) or getattr(error, "winerror", None) in {5, 32, 33}
            if not retryable or attempt == len(retry_delays):
                raise
            time.sleep(retry_delays[attempt])


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
    replace_with_retry(temporary, path)


def write_jsonl(path: Path, events: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    with temporary.open("w", encoding="utf-8") as stream:
        for event in events:
            stream.write(json.dumps(event, ensure_ascii=False, separators=(",", ":"), sort_keys=True))
            stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    replace_with_retry(temporary, path)


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
        session_dir / "edit-path" / "events.jsonl",
        session_dir / "evidence" / "raw-events.jsonl",
        session_dir / "trajectory.jsonl",
        session_dir / "raw-events.jsonl",
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    # Crash-recovered sessions retain numbered JSONL segments.  Prefer the
    # canonical assembled trajectory when present, but make the multi-file
    # evidence discoverable to callers that only know the session directory.
    numbered = sorted(session_dir.glob("raw-events-*.jsonl"))
    if not numbered:
        numbered = sorted((session_dir / "EDIT-PATH").glob("events-*.jsonl"))
    if numbered:
        from .segments import assemble_segments
        assembled = session_dir / "trajectory.jsonl"
        # assemble_segments understands both the legacy root-level journals
        # and the recorder's durable EDIT-PATH layout.  Do not mirror
        # EDIT-PATH files into the root: doing so changes the reference base
        # before assembly and turns valid ``../states`` paths into stale ones.
        assemble_segments(session_dir, assembled)
        return assembled
    raise EditPathError(f"no trajectory JSONL found under {session_dir}")


def safe_relative(path: str) -> Path:
    value = Path(path)
    if not value.parts or value.is_absolute() or ".." in value.parts:
        raise EditPathError(f"unsafe bundle path: {path!r}")
    return value
