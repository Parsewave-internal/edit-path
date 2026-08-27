import tempfile
import unittest
from pathlib import Path

from edit_path.reasoning import align_reasoning, append_reasoning_record


class ReasoningJournalTests(unittest.TestCase):
    def test_record_is_durable_under_edit_path(self):
        with tempfile.TemporaryDirectory() as td:
            session = Path(td)
            append_reasoning_record(session, {"schema_version": "edit-path/reasoning@1", "reasoning_segment_id": "r1"})
            path = session / "EDIT-PATH" / "reasoning-events.jsonl"
            self.assertTrue(path.is_file())
            self.assertIn('"reasoning_segment_id":"r1"', path.read_text())

    def test_alignment_attaches_nearest_events(self):
        record = {"started_monotonic_ns": 20, "ended_monotonic_ns": 40}
        events = [{"event_id": "a", "monotonic_ns": 10}, {"event_id": "b", "monotonic_ns": 30}, {"event_id": "c", "monotonic_ns": 50}]
        aligned = align_reasoning(record, events)
        self.assertEqual(aligned["previous_event_id"], "a")
        self.assertEqual(aligned["overlapping_event_ids"], ["b"])
        self.assertEqual(aligned["next_event_id"], "c")


if __name__ == "__main__":
    unittest.main()
