# SPDX-FileCopyrightText: 2026 Edit Path contributors
# SPDX-License-Identifier: GPL-3.0-only

from __future__ import annotations

import os
import platform
import shutil
import subprocess
from pathlib import Path
from typing import Any

from .errors import GateError
from .io import write_json


def _tool(binary: str | None, alternatives: tuple[str, ...], *, version_flag: str = "--version") -> dict[str, str | None]:
    executable = binary or next((shutil.which(name) for name in alternatives if shutil.which(name)), None)
    if not executable:
        return {"executable": None, "version": None}
    completed = subprocess.run([executable, version_flag], text=True, capture_output=True)
    first_line = (completed.stdout or completed.stderr).splitlines()
    return {
        "executable": Path(executable).name,
        "version": first_line[0].strip() if completed.returncode == 0 and first_line else None,
    }


def runtime_fingerprint(*, melt_binary: str | None = None) -> dict[str, Any]:
    return {
        "schema": "video-path/runtime-lock@1",
        "container_image": os.environ.get("EDIT_PATH_CONTAINER_IMAGE"),
        "platform": platform.platform(),
        "tools": {
            "melt": _tool(melt_binary, ("melt", "mlt-melt")),
            "ffmpeg": _tool(None, ("ffmpeg",), version_flag="-version"),
            "ffprobe": _tool(None, ("ffprobe",), version_flag="-version"),
        },
    }


def write_runtime_lock(path: Path, *, melt_binary: str | None = None) -> dict[str, Any]:
    value = runtime_fingerprint(melt_binary=melt_binary)
    missing = [name for name, tool in value["tools"].items() if not tool.get("version")]
    if missing:
        raise GateError("runtime_pin", f"cannot lock a runtime with missing tools: {', '.join(missing)}")
    write_json(path, value)
    return value


def verify_runtime_lock(expected: dict[str, Any], actual: dict[str, Any]) -> None:
    if expected.get("schema") != "video-path/runtime-lock@1":
        raise GateError("runtime_pin", "unsupported reconstruction runtime lock schema")
    for name in ("melt", "ffmpeg", "ffprobe"):
        expected_version = expected.get("tools", {}).get(name, {}).get("version")
        actual_version = actual.get("tools", {}).get(name, {}).get("version")
        if not expected_version or expected_version != actual_version:
            raise GateError("runtime_pin", f"{name} version mismatch: expected {expected_version!r}, found {actual_version!r}")
    expected_image = expected.get("container_image")
    if expected_image and expected_image != actual.get("container_image"):
        raise GateError("runtime_pin", "container image digest does not match the runtime lock")
