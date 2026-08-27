import tempfile
import unittest
from pathlib import Path

from edit_path.audio_capture import AudioCapture


class AudioCaptureTests(unittest.TestCase):
    def test_platform_command_writes_flac_to_reasoning_folder(self):
        with tempfile.TemporaryDirectory() as td:
            capture = AudioCapture(Path(td), ffmpeg="ffmpeg", segment_seconds=30)
            command = capture.command(Path(td) / "EDIT-PATH/reasoning/audio-001.flac")
            self.assertIn("-c:a", command)
            self.assertEqual(command[-1].replace("\\", "/").split("/")[-1], "audio-001.flac")

    def test_missing_ffmpeg_is_recorded_not_raised(self):
        with tempfile.TemporaryDirectory() as td:
            def fail(*args, **kwargs):
                raise OSError("ffmpeg missing")
            capture = AudioCapture(Path(td), popen=fail)
            self.assertIsNone(capture.start_segment())
            self.assertTrue((Path(td) / "EDIT-PATH/reasoning/capture-error.json").is_file())

