import tempfile
import unittest
from pathlib import Path

from edit_path.transcription import TranscriptionWorker


class TranscriptionTests(unittest.TestCase):
    def test_provider_runs_off_caller_and_persists_literal_result(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); audio = root / "audio.flac"; audio.touch()
            worker = TranscriptionWorker(root, lambda path: {"text": "human words", "language": "en", "words": []})
            result = worker.submit(audio, {"reasoning_segment_id": "r1"}).result(timeout=2); worker.close()
            self.assertEqual(result["transcript"]["text"], "human words")
            self.assertTrue((root / "EDIT-PATH/reasoning/transcript-r1.json").is_file())

    def test_provider_failure_is_recorded(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); worker = TranscriptionWorker(root, lambda _: (_ for _ in ()).throw(RuntimeError("device")))
            result = worker.submit(root / "missing.flac", {"reasoning_segment_id": "r2"}).result(timeout=2); worker.close()
            self.assertIsNone(result["transcript"]); self.assertIn("device", result["transcription_error"])

