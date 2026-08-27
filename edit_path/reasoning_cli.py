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
    for record in result["records"]:
        aligned = align_reasoning(record, events)
        write_json(session / "EDIT-PATH" / "reasoning" / f"transcript-{record['reasoning_segment_id']}.json", aligned)
        record.update(aligned)
    print(json.dumps(result, ensure_ascii=False)); return 0
if __name__ == "__main__": raise SystemExit(main())
