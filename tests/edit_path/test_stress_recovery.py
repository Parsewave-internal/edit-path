import json
import tempfile
import unittest
from pathlib import Path

from edit_path.errors import EditPathError
from edit_path.io import read_jsonl, write_jsonl
from edit_path.segments import assemble_segments
from edit_path.state import canonical_hash


class RecoveryStressTests(unittest.TestCase):
    def test_multiple_crash_segments_are_synthesized(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            snapshot = {"tracks": [], "clips": []}
            for n in (1, 2, 3):
                sid = "shared-session"
                events = [{"schema_version": "0.1.0", "event_type": "session.start", "sequence": 1, "session_id": sid, "event_id": f"start-{n}"},
                          {"schema_version": "0.1.0", "event_type": "state.checkpoint", "sequence": 2, "session_id": sid, "event_id": f"checkpoint-{n}", "timeline_id": "main", "snapshot": snapshot, "state_hash": canonical_hash(snapshot)},
                          {"schema_version": "0.1.0", "event_type": "session.end" if n == 3 else "ui.action", "sequence": 3, "session_id": sid, "event_id": f"end-{n}"}]
                write_jsonl(root / f"raw-events-{n:03d}.jsonl", events)
            result = assemble_segments(root, root / "trajectory.jsonl")
            self.assertEqual(result["segments"], 3)
            self.assertGreater(len(read_jsonl(root / "trajectory.jsonl")), 8)

    def test_truncated_json_is_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "bad.jsonl"
            path.write_text('{"event_type":"x"}\n{"broken":', encoding="utf-8")
            with self.assertRaises(EditPathError): read_jsonl(path)

    def test_atomic_jsonl_write_round_trips_unicode(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "events.jsonl"
            write_jsonl(path, [{"text": "剪辑 🎬", "sequence": 1}])
            self.assertEqual(read_jsonl(path)[0]["text"], "剪辑 🎬")

    def test_large_event_payload_round_trips(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "large.jsonl"
            payload = {"event_type": "ui", "data": "x" * 1_000_000}
            write_jsonl(path, [payload])
            self.assertEqual(len(read_jsonl(path)[0]["data"]), 1_000_000)

    def test_cache_write_leaves_no_temporary_files(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); path = root / "events.jsonl"
            write_jsonl(path, [{"sequence": 1}])
            self.assertFalse(list(root.glob(".events.jsonl.tmp-*")))
