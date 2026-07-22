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
from sample_collector import bind_project_assets
from validate_sample import validate_sample
from validate_video_path import validate as validate_raw

HASH_B = "b" * 64


class MvpTests(unittest.TestCase):
    def test_project_asset_binding_requires_path_or_content_identity(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "assets").mkdir()
            bundled = root / "assets" / "content.mp4"
            bundled.write_bytes(b"expected media")
            digest = hashlib.sha256(bundled.read_bytes()).hexdigest()
            asset = {"asset_id": "asset_001", "file": "assets/content.mp4", "original_filename": "source.mp4", "sha256": digest, "bytes": bundled.stat().st_size}

            identical = root / "renamed.mp4"
            identical.write_bytes(bundled.read_bytes())
            project = root / "project.kdenlive"
            project.write_text(f'<mlt><producer><property name="kdenlive:id">4</property><property name="resource">{identical}</property></producer></mlt>')
            self.assertEqual(bind_project_assets(project, root, [asset])[0]["bin_reference"], "4")

            wrong = root / "source.mp4"
            wrong.write_bytes(b"different media")
            project.write_text(f'<mlt><producer><property name="kdenlive:id">4</property><property name="resource">{wrong}</property></producer></mlt>')
            with self.assertRaisesRegex(ValueError, "could not bind exactly one"):
                bind_project_assets(project, root, [asset])

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
            (root / "internal/rationale.jsonl").write_text(json.dumps({"reason": "pace", "decision": "shorter opening"}) + "\n")
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
                "sample_id": "sample_test", "prompt": "Make a short edit", "editor_plan": "Use the strongest shot",
                "editor": {"editor_id": "editor_test"},
                "project": {"frame_rate": {"numerator": 25, "denominator": 1}, "width": 1920, "height": 1080},
                "assets": [{"asset_id": "asset_001", "original_filename": "source.mp4", "file": "assets/asset_001.mp4",
                            "sha256": sha(root / "assets/asset_001.mp4"), "bytes": 5}],
                "asset_binding_method": "first_use_order", "editor_review": "Checked",
                "artifacts": {"final_video": "output/final.mp4", "final_video_sha256": sha(root / "output/final.mp4"),
                    "native_project": "internal/final.kdenlive", "native_project_sha256": sha(root / "internal/final.kdenlive"),
                    "raw_events": "evidence/raw-events.jsonl", "raw_events_sha256": sha(raw)}}
            sample = build_sample(root, metadata)
            self.assertEqual(sample["edit_path"]["operations"][0]["operation"], "clip.insert")
            path = root / "sample.json"
            path.write_text(json.dumps(sample))
            self.assertEqual(validate_sample(path, check_files=True), [])

    def test_v03_structural_contract(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "session.jsonl"
            p0, p1 = "a" * 64, "b" * 64
            base = {"schema_version": "0.3.0", "session_id": "session", "timestamp_utc": "2026-07-22T00:00:00Z"}
            events = [
                {**base, "sequence": 1, "event_id": "1", "event_type": "session.start"},
                {**base, "sequence": 2, "event_id": "2", "event_type": "project.context", "context": {
                    "project_id": "project", "fps_numerator": 25, "fps_denominator": 1, "width": 1920, "height": 1080,
                    "sample_aspect_numerator": 1, "sample_aspect_denominator": 1, "display_aspect_numerator": 16,
                    "display_aspect_denominator": 9, "colorspace": 709, "progressive": True, "bottom_field_first": False,
                    "audio_channels": 2, "audio_sample_rate": None, "kdenlive_version": "test", "kdenlive_build": "test", "mlt_version": "test",
                }},
                {**base, "sequence": 3, "event_id": "3", "event_type": "state.checkpoint", "timeline_id": "timeline", "label": "baseline",
                    "state_hash": HASH_B, "snapshot": {}, "project_state": {"encoding": "zstd", "path": "states/a.zst", "sha256": p0, "bytes": 10},
                    "reference_proxy": {"path": "refs/a.mp4"}},
                {**base, "sequence": 4, "event_id": "4", "event_type": "state.diff", "timeline_id": "timeline", "label": "edit",
                    "boundary": "commit", "transaction_id": "transaction", "undo_entry_id": "entry", "before_hash": HASH_B, "after_hash": "c" * 64,
                    "project_before_hash": p0, "project_after_hash": p1, "project_state": {"encoding": "zstd", "path": "states/b.zst", "sha256": p1, "bytes": 10},
                    "diff": {"changes": []}},
                {**base, "sequence": 5, "event_id": "5", "event_type": "session.end", "state_sidecars_complete": True},
            ]
            path.write_text("".join(json.dumps(value) + "\n" for value in events), encoding="utf-8")
            self.assertEqual(validate_raw(path), [])


if __name__ == "__main__":
    unittest.main()
