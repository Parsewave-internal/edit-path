import unittest

from edit_path.reasoning import transcript_to_vtt


class TranscriptTests(unittest.TestCase):
    def test_vtt_preserves_literal_transcript_and_timing(self):
        value = transcript_to_vtt([{"started_monotonic_ns": 1_000_000_000, "ended_monotonic_ns": 2_500_000_000,
                                    "transcript": {"text": "I am trimming this clip."}}])
        self.assertIn("00:00:00.000 --> 00:00:01.500", value)
        self.assertIn("I am trimming this clip.", value)
