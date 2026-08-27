import tempfile
import unittest
from pathlib import Path

from edit_path.reasoning_session import ReasoningSession


class FakeCapture:
    def __init__(self, session): self.session, self.stopped = session, False
    def start_segment(self):
        path = self.session / "EDIT-PATH/reasoning/audio-001.flac"; path.parent.mkdir(parents=True, exist_ok=True); path.touch(); return path
    def stop(self): self.stopped = True


class ReasoningSessionTests(unittest.TestCase):
    def test_explicit_start_stop_persists_aligned_record(self):
        with tempfile.TemporaryDirectory() as td:
            session = Path(td); controller = ReasoningSession(session, session_id="s1", capture=FakeCapture(session))
            self.assertTrue(controller.start())
            record = controller.stop(events=[{"event_id": "e1", "monotonic_ns": controller.started_ns + 1}])
            self.assertEqual(record["overlapping_event_ids"], ["e1"])
            self.assertTrue((session / "EDIT-PATH/reasoning-events.jsonl").is_file())

    def test_toggle_is_idempotent(self):
        with tempfile.TemporaryDirectory() as td:
            controller = ReasoningSession(Path(td), session_id="s1", capture=FakeCapture(Path(td)))
            self.assertTrue(controller.start()); self.assertFalse(controller.start()); self.assertIsNotNone(controller.stop()); self.assertIsNone(controller.stop());
