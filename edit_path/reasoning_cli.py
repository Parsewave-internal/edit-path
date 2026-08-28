"""CLI bridge used by the recorder GUI after Stop Reasoning."""
from __future__ import annotations
import argparse, json
from pathlib import Path
from .io import read_jsonl
from .reasoning_pipeline import transcribe_reasoning_segments
from .whisper_provider import transcribe_with_whisper
from .reasoning import align_reasoning
from .io import write_json

def main() -> int:
    p=argparse.ArgumentParser(); p.add_argument("session", type=Path); p.add_argument("audio", type=Path); args=p.parse_args()
    session=args.session.resolve(); audio=args.audio.resolve()
    events=[]
    for candidate in sorted((session/"EDIT-PATH").glob("events-*.jsonl")):
        try: events.extend(read_jsonl(candidate))
        except Exception: pass
    result=transcribe_reasoning_segments(session,[audio],provider=lambda path: transcribe_with_whisper(path),metadata={"event_count":len(events)})
    aligned_records = []
    event_ids = {e.get("event_id") for e in events if e.get("event_id")}
    for record in result["records"]:
        aligned = align_reasoning(record, events)
        referenced = [x for x in aligned.get("overlapping_event_ids", []) if x in event_ids]
        aligned["overlapping_event_ids"] = referenced
        write_json(session / "EDIT-PATH" / "reasoning" / f"transcript-{record['reasoning_segment_id']}.json", aligned)
        aligned_records.append(aligned)
    write_json(session / "EDIT-PATH" / "reasoning" / "reasoning.json", {
        "schema_version": "edit-path/reasoning-bundle@1",
        "session_id": next((e.get("session_id") for e in events if e.get("session_id")), str(session.name)),
        "audio_file": str(audio.relative_to(session)).replace("\\", "/"),
        "event_count": len(events),
        "segments": aligned_records,
    })
    print(json.dumps(result, ensure_ascii=False)); return 0
if __name__ == "__main__": raise SystemExit(main())
