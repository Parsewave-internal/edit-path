import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from edit_path.whisper_provider import transcribe_with_whisper


class WhisperProviderTests(unittest.TestCase):
    def test_literal_transcript_is_not_rewritten(self):
        with tempfile.TemporaryDirectory() as td:
            audio = Path(td) / "reasoning.flac"; audio.touch()
            def fake_run(command, **_):
                out = Path(command[command.index("--output_dir") + 1]) / "reasoning.json"
                out.write_text(json.dumps({"text": "I'm cutting this clip because it feels too slow."}), encoding="utf-8")
                return mock.Mock(returncode=0, stdout="", stderr="")
            with mock.patch("edit_path.whisper_provider.subprocess.run", side_effect=fake_run):
                result = transcribe_with_whisper(audio, whisper_binary="whisper")
            self.assertEqual(result["text"], "I'm cutting this clip because it feels too slow.")

    def test_diagnostics_are_written_to_the_recording_session(self):
        with tempfile.TemporaryDirectory() as td:
            session = Path(td) / "nested" / "session"
            audio = session / "EDIT-PATH" / "reasoning" / "reasoning.flac"
            audio.parent.mkdir(parents=True)
            audio.touch()

            def fake_run(command, **_):
                out = Path(command[command.index("--output_dir") + 1]) / "reasoning.json"
                out.write_text(json.dumps({"text": "literal words"}), encoding="utf-8")
                return mock.Mock(returncode=0, stdout="", stderr="")

            with (
                mock.patch("edit_path.whisper_provider.subprocess.run", side_effect=fake_run),
                mock.patch("edit_path.whisper_provider.log_event") as log_event,
            ):
                transcribe_with_whisper(audio, whisper_binary="whisper")

            self.assertEqual(log_event.call_args_list[0].args[:2], (session, "transcription_start"))

    def test_provider_failure_is_actionable(self):
        with tempfile.TemporaryDirectory() as td:
            audio = Path(td) / "reasoning.flac"; audio.touch()
            with mock.patch("edit_path.whisper_provider.subprocess.run", return_value=mock.Mock(returncode=2, stdout="", stderr="bad model")):
                with self.assertRaisesRegex(RuntimeError, "Whisper failed"):
                    transcribe_with_whisper(audio, whisper_binary="whisper")

    def test_missing_json_is_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            audio = Path(td) / "reasoning.flac"; audio.touch()
            with mock.patch("edit_path.whisper_provider.subprocess.run", return_value=mock.Mock(returncode=0, stdout="", stderr="")):
                with self.assertRaisesRegex(RuntimeError, "without producing"):
                    transcribe_with_whisper(audio, whisper_binary="whisper")

    def test_python_module_fallback_runs_when_cli_is_not_on_path(self):
        with tempfile.TemporaryDirectory() as td:
            audio = Path(td) / "reasoning.flac"
            audio.touch()
            calls = []

            def fake_run(command, **_):
                calls.append(command)
                if command[-2:] == ["whisper", "--help"]:
                    return mock.Mock(returncode=0, stdout="", stderr="")
                out = Path(command[command.index("--output_dir") + 1]) / "reasoning.json"
                out.write_text(json.dumps({"text": "module fallback transcript"}), encoding="utf-8")
                return mock.Mock(returncode=0, stdout="", stderr="")

            with (
                mock.patch("edit_path.whisper_provider.shutil.which", return_value=None),
                mock.patch("edit_path.whisper_provider.subprocess.run", side_effect=fake_run),
            ):
                result = transcribe_with_whisper(audio)
            self.assertEqual(result["text"], "module fallback transcript")
            self.assertEqual(calls[0][-2:], ["whisper", "--help"])
            self.assertIn("-m", calls[1])
