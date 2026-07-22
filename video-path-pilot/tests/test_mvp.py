# SPDX-FileCopyrightText: 2026 Video Path Pilot contributors
# SPDX-License-Identifier: GPL-3.0-only

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1]))
from normalize_sample import accepted_commits, build_sample
from validate_sample import validate_sample

HASH_B = "b" * 64


class MvpTests(unittest.TestCase):
    def test_undo_is_removed_and_redo_is_restored(self):
        a = {"event_type": "state.diff", "boundary": "commit", "event_id": "a"}
        b = {"event_type": "state.diff", "boundary": "commit", "event_id": "b"}
        undo = {"event_type": "state.diff", "boundary": "undo"}
        redo = {"event_type": "state.diff", "boundary": "redo"}
        self.assertEqual(accepted_commits([a, b, undo]), [a])
        self.assertEqual(accepted_commits([a, b, undo, redo]), [a, b])

    def test_build_and_validate_sample(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for directory in ("assets", "output", "internal", "evidence"):
                (root / directory).mkdir()
            (root / "assets/asset_001.mp4").write_bytes(b"asset")
            (root / "output/final.mp4").write_bytes(b"video")
            (root / "internal/final.kdenlive").write_bytes(b"project")
            events = [{"event_type": "session.start", "sequence": 1}, {
                "event_type": "state.diff", "boundary": "commit", "sequence": 2,
                "event_id": "raw-2", "label": "Insert Clip", "after_hash": HASH_B,
                "diff": {"changes": [{"entity": "clip", "native_id": 8, "change": "added",
                    "after": {"asset_reference": "4", "track_native_id": 3, "timeline_start_frame": 0, "duration_frames": 25}}]},
            }, {"event_type": "session.end", "sequence": 3}]
            raw = root / "evidence/raw-events.jsonl"
            raw.write_text("".join(json.dumps(e) + "\n" for e in events))
            sha = lambda path: hashlib.sha256(path.read_bytes()).hexdigest()
            metadata = {
                "sample_id": "sample_test", "prompt": "Make a short edit",
                "editor": {"editor_id": "editor_test"},
                "project": {"frame_rate": {"numerator": 25, "denominator": 1}, "width": 1920, "height": 1080},
                "assets": [{"asset_id": "asset_001", "original_filename": "source.mp4", "file": "assets/asset_001.mp4",
                            "sha256": sha(root / "assets/asset_001.mp4"), "bytes": 5}],
                "asset_binding_method": "first_use_order", "output_completion_confirmed": True,
                "artifacts": {"final_video": "output/final.mp4", "final_video_sha256": sha(root / "output/final.mp4"),
                    "native_project": "internal/final.kdenlive", "native_project_sha256": sha(root / "internal/final.kdenlive"),
                    "raw_events": "evidence/raw-events.jsonl", "raw_events_sha256": sha(raw)}}
            sample = build_sample(root, metadata)
            self.assertEqual(sample["edit_path"]["operations"][0]["operation"], "clip.insert")
            self.assertNotIn("rationale", sample)
            self.assertNotIn("editor_plan", sample["task"])
            path = root / "sample.json"
            path.write_text(json.dumps(sample))
            self.assertEqual(validate_sample(path, check_files=True), [])


if __name__ == "__main__":
    unittest.main()
