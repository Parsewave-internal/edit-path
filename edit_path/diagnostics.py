"""Structured, redacted diagnostics for supportable EditPath failures."""
from __future__ import annotations

import json
import os
import platform
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def log_path(session: Path) -> Path:
    path = session / "EDIT-PATH" / "diagnostics.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def log_event(session: Path, event: str, **fields: Any) -> None:
    """Append a durable diagnostic event; never raise into the editing path."""
    try:
        safe = {"event": event, "timestamp_utc": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
                "pid": os.getpid(), "platform": platform.platform(), "python": sys.version.split()[0]}
        safe.update({k: ("<redacted>" if any(token in k.lower() for token in ("key", "token", "secret", "password")) else v)
                     for k, v in fields.items()})
        with log_path(session).open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(safe, ensure_ascii=False, sort_keys=True) + "\n")
    except Exception:
        pass


def log_exception(session: Path, event: str, error: BaseException, **fields: Any) -> None:
    log_event(session, event, error_type=type(error).__name__, error=str(error), traceback=traceback.format_exc(), **fields)
