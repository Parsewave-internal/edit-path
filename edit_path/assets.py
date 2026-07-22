# SPDX-FileCopyrightText: 2026 Edit Path contributors
# SPDX-License-Identifier: GPL-3.0-only

from __future__ import annotations

import os
import shutil
import urllib.parse
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Iterable

from .errors import EditPathError, GateError
from .io import safe_relative, sha256_file, write_json


RESOURCE_PROPERTIES = {"resource", "warp_resource", "kdenlive:originalurl", "kdenlive:proxy"}
NON_FILE_PREFIXES = (
    "avformat:",
    "color:",
    "colour:",
    "consumer:",
    "frei0r.",
    "kdenlivetitle:",
    "qtext:",
    "tractor:",
    "xml:",
)


def _path_from_resource(value: str, project_dir: Path) -> Path | None:
    value = value.strip()
    if not value or value.startswith(NON_FILE_PREFIXES):
        return None
    if value.startswith("file://"):
        value = urllib.parse.unquote(urllib.parse.urlparse(value).path)
    if value.startswith("timewarp:"):
        parts = value.split(":", 2)
        value = parts[2] if len(parts) == 3 else ""
    candidate = Path(os.path.expandvars(os.path.expanduser(value)))
    if not candidate.is_absolute():
        candidate = project_dir / candidate
    candidate = candidate.resolve()
    return candidate if candidate.is_file() else None


def discover_assets(project_xml: bytes, project_path: Path) -> list[Path]:
    try:
        root = ET.fromstring(project_xml)
    except ET.ParseError as exc:
        raise EditPathError(f"invalid Kdenlive/MLT project XML: {exc}") from exc
    found: dict[str, Path] = {}
    for element in root.iter():
        values: list[str] = []
        if element.tag == "property" and element.get("name") in RESOURCE_PROPERTIES and element.text:
            values.append(element.text)
        for attribute in ("resource", "src"):
            if element.get(attribute):
                values.append(element.get(attribute, ""))
        for value in values:
            candidate = _path_from_resource(value, project_path.parent)
            if candidate:
                found[str(candidate)] = candidate
    return sorted(found.values(), key=lambda path: str(path))


def create_manifest(root: Path, assets: Iterable[Path], *, license_status: str = "pending") -> dict:
    asset_dir = root / "assets"
    asset_dir.mkdir(parents=True, exist_ok=True)
    records = []
    seen: dict[str, str] = {}
    for index, source_value in enumerate(assets, 1):
        source = source_value.expanduser().resolve()
        if not source.is_file():
            raise EditPathError(f"input asset not found: {source_value}")
        digest = sha256_file(source)
        relative = seen.get(digest)
        if relative is None:
            safe_name = source.name.replace("/", "_").replace("\\", "_")
            relative = f"assets/{digest[:16]}-{safe_name}"
            destination = root / relative
            if source != destination.resolve():
                shutil.copy2(source, destination)
            seen[digest] = relative
        records.append({
            "asset_id": f"asset_{index:03d}",
            "original_filename": source.name,
            "file": relative,
            "sha256": digest,
            "bytes": source.stat().st_size,
            "license_status": license_status,
        })
    manifest = {"schema": "video-path/assets@2", "assets": records}
    write_json(root / "asset-manifest.json", manifest)
    return manifest


def load_manifest(session_dir: Path) -> tuple[Path, dict]:
    candidates = (
        session_dir / "asset-manifest.json",
        session_dir / "assets.json",
        session_dir / "internal" / "collector-metadata.json",
    )
    for path in candidates:
        if not path.is_file():
            continue
        import json

        value = json.loads(path.read_text(encoding="utf-8"))
        if path.name == "collector-metadata.json":
            value = {"schema": "video-path/assets@1", "assets": value.get("assets", [])}
        return path, value
    raise GateError("assets", "asset manifest is missing")


def verify_assets(session_dir: Path, manifest: dict, *, require_approved_license: bool = False) -> list[dict]:
    assets = manifest.get("assets")
    if not isinstance(assets, list) or not assets:
        raise GateError("assets", "asset manifest contains no assets")
    verified = []
    for index, entry in enumerate(assets):
        relative_value = entry.get("file", entry.get("path", ""))
        relative = safe_relative(str(relative_value))
        asset = session_dir / relative
        if asset.is_symlink() or not asset.is_file() or not asset.resolve().is_relative_to(session_dir.resolve()):
            raise GateError("assets", f"asset {index} is missing: {relative}")
        if asset.stat().st_size != entry.get("bytes"):
            raise GateError("assets", f"asset {index} byte count mismatch: {relative}")
        if sha256_file(asset) != entry.get("sha256"):
            raise GateError("assets", f"asset {index} SHA-256 mismatch: {relative}")
        if require_approved_license and entry.get("license_status") != "approved":
            raise GateError("asset_license", f"asset {index} is not approved for publication")
        verified.append(entry)
    return verified


def remap_project_assets(
    project_xml: bytes,
    project_path: Path,
    manifest: dict,
    session_dir: Path,
    *,
    absolute_paths: bool = True,
) -> bytes:
    try:
        root = ET.fromstring(project_xml)
    except ET.ParseError as exc:
        raise EditPathError(f"invalid trajectory project XML: {exc}") from exc

    mapping: dict[str, str] = {}
    for entry in manifest.get("assets", []):
        relative = entry.get("file", entry.get("path"))
        if not relative:
            continue
        relative_path = safe_relative(str(relative))
        source_in_session = (session_dir / relative_path).resolve()
        bundled = str(source_in_session) if absolute_paths else relative_path.as_posix()
        for source in (relative_path.as_posix(), str(source_in_session), source_in_session.as_uri()):
            mapping[source] = bundled
        for source_key in ("source", "original_path", "original_filename"):
            source = entry.get(source_key)
            if source:
                mapping[str(source)] = bundled
                try:
                    mapping[Path(str(source)).as_uri()] = bundled
                except ValueError:
                    pass

    by_name = {
        Path(str(entry.get("original_filename", ""))).name: (
            str((session_dir / safe_relative(str(entry.get("file", entry.get("path", ""))))).resolve())
            if absolute_paths
            else safe_relative(str(entry.get("file", entry.get("path", "")))).as_posix()
        )
        for entry in manifest.get("assets", [])
        if entry.get("original_filename") and entry.get("file", entry.get("path"))
    }

    def rewrite(value: str) -> str:
        stripped = value.strip()
        if stripped in mapping:
            return mapping[stripped]
        if stripped.startswith("timewarp:"):
            parts = stripped.split(":", 2)
            if len(parts) == 3:
                mapped = mapping.get(parts[2]) or by_name.get(Path(parts[2]).name)
                if mapped:
                    return f"timewarp:{parts[1]}:{mapped}"
        rewritten = by_name.get(Path(urllib.parse.unquote(stripped)).name)
        if rewritten:
            return rewritten
        candidate = _path_from_resource(stripped, project_path.parent)
        if candidate is not None:
            raise EditPathError(f"project references an unmanifested asset: {candidate}")
        return value

    for element in root.iter():
        if element.tag == "property" and element.get("name") in RESOURCE_PROPERTIES and element.text:
            element.text = rewrite(element.text)
        for attribute in ("resource", "src"):
            if element.get(attribute):
                element.set(attribute, rewrite(element.get(attribute, "")))
    root.set("root", ".")
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)
