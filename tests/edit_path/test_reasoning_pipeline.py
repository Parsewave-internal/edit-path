import tempfile
import unittest
from pathlib import Path

from edit_path.reasoning_pipeline import transcribe_reasoning_segments


class ReasoningPipelineTests(unittest.TestCase):
    def test_literal_speech_becomes_caption_without_rationale_rewrite(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); audio = root / "EDIT-PATH/reasoning/audio-001.flac"
            audio.parent.mkdir(parents=True); audio.touch()
            result = transcribe_reasoning_segments(root, [audio], provider=lambda _: {
                "text": "I'm cutting this clip because it feels too slow.", "segments": []})
            self.assertEqual(result["transcribed"], 1)
            self.assertIn("I'm cutting this clip because it feels too slow.",
                          (root / "EDIT-PATH/reasoning/captions.vtt").read_text())

    def test_provider_failure_keeps_caption_file_and_error_record(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); audio = root / "EDIT-PATH/reasoning/audio-001.flac"
            audio.parent.mkdir(parents=True); audio.touch()
            result = transcribe_reasoning_segments(root, [audio], provider=lambda _: (_ for _ in ()).throw(RuntimeError("offline")))
            self.assertEqual(result["transcribed"], 0)
            self.assertTrue((root / "EDIT-PATH/reasoning/captions.vtt").is_file())
            self.assertIn("transcription_error", (root / "EDIT-PATH/reasoning/transcript-audio-001.json").read_text())
