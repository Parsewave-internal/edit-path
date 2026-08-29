"""Create a redacted diagnostics bundle for Windows support."""
from __future__ import annotations
import argparse, json, zipfile
from pathlib import Path

def create_bundle(session: Path, output: Path) -> Path:
    session = session.resolve(); output.parent.mkdir(parents=True, exist_ok=True)
    allowed = {"diagnostics.jsonl", "supervisor-activity.log", "session.json", "rejection.json"}
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in session.rglob("*"):
            if path.is_file() and (path.name in allowed or path.parts[-2:] == ("verification", "report.json")):
                data = path.read_bytes()
                if path.name == "session.json":
                    try:
                        obj = json.loads(data); obj.pop("kdenlive_pid", None); data = (json.dumps(obj, indent=2) + "\n").encode()
                    except Exception: pass
                archive.writestr(path.relative_to(session).as_posix(), data)
    return output

def main() -> int:
    parser = argparse.ArgumentParser(); parser.add_argument("session", type=Path); parser.add_argument("output", type=Path)
    args = parser.parse_args(); print(create_bundle(args.session, args.output)); return 0

if __name__ == "__main__": raise SystemExit(main())
