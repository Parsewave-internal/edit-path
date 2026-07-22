# SPDX-FileCopyrightText: 2026 Video Path Pilot contributors
# SPDX-License-Identifier: GPL-3.0-only

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1]))
from job_pipeline import canonical_hash, replay_report, resolve_assets
from normalize_sample import accepted_commits, build_sample
from validate_sample import validate_sample

HASH_B = "b" * 64


class MvpTests(unittest.TestCase):
    def test_legacy_final_branch_helper(self):
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
                "event_type": "state.checkpoint", "sequence": 2, "snapshot": {"timeline_id": "timeline",
                "duration_frames": 0, "tracks": [], "clips": [], "compositions": [], "mixes": [],
                "master_effects": [{"native_id": 0, "effects": []}]}, "state_hash": "a" * 64}, {
                "event_type": "state.diff", "boundary": "commit", "sequence": 3,
                "event_id": "raw-2", "label": "Insert Clip", "after_hash": HASH_B,
                "diff": {"changes": [{"entity": "clip", "native_id": 8, "change": "added",
                    "after": {"asset_reference": "4", "track_native_id": 3, "timeline_start_frame": 0, "duration_frames": 25}}]},
            }, {"event_type": "session.end", "sequence": 4}]
            added = events[2]["diff"]["changes"][0]["after"]
            events[3:3] = [{
                "event_type": "state.diff", "boundary": "undo", "sequence": 4,
                "event_id": "raw-3", "label": "Undo Insert Clip", "after_hash": "c" * 64,
                "diff": {"changes": [{"entity": "clip", "native_id": 8, "change": "removed", "before": added}]},
            }, {
                "event_type": "state.diff", "boundary": "redo", "sequence": 5,
                "event_id": "raw-4", "label": "Redo Insert Clip", "after_hash": HASH_B,
                "diff": {"changes": [{"entity": "clip", "native_id": 8, "change": "added", "after": added}]},
            }]
            raw = root / "evidence/raw-events.jsonl"
            raw.write_text("".join(json.dumps(e) + "\n" for e in events))
            sha = lambda path: hashlib.sha256(path.read_bytes()).hexdigest()
            metadata = {
                "sample_id": "sample_test", "prompt": "Make a short edit",
                "editor": {"editor_id": "editor_test"},
                "project": {"frame_rate": {"numerator": 25, "denominator": 1}, "width": 1920, "height": 1080},
                "assets": [{"asset_id": "asset_001", "original_filename": "source.mp4", "file": "assets/asset_001.mp4",
                            "sha256": sha(root / "assets/asset_001.mp4"), "bytes": 5}],
                "asset_binding_method": "project_resource_sha256", "native_asset_bindings": {"4": "asset_001"},
                "output_completion_confirmed": True,
                "artifacts": {"final_video": "output/final.mp4", "final_video_sha256": sha(root / "output/final.mp4"),
                    "native_project": "internal/final.kdenlive", "native_project_sha256": sha(root / "internal/final.kdenlive"),
                    "raw_events": [{"file": "evidence/raw-events.jsonl", "sha256": sha(raw)}]}}
            sample = build_sample(root, metadata)
            self.assertEqual([operation["operation"] for operation in sample["edit_path"]["operations"]],
                             ["clip.insert", "history.undo", "history.redo"])
            self.assertTrue(sample["quality"]["undo_redo_preserved_in_edit_path"])
            self.assertNotIn("rationale", sample)
            self.assertNotIn("editor_plan", sample["task"])
            path = root / "sample.json"
            path.write_text(json.dumps(sample))
            self.assertEqual(validate_sample(path, check_files=True), [])

    def test_project_resources_resolve_by_hash_not_import_order(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "assets").mkdir()
            first = root / "assets/asset_001.wav"; first.write_bytes(b"audio")
            second = root / "assets/asset_002.mp4"; second.write_bytes(b"video")
            project = root / "final.kdenlive"
            project.write_text(f'''<mlt root="{root}">
              <profile frame_rate_num="25" frame_rate_den="1" width="1920" height="1080"/>
              <chain id="chain0"><property name="resource">{second}</property>
              <property name="kdenlive:id">4</property><property name="mlt_service">avformat</property></chain>
            </mlt>''')
            make = lambda path, asset_id: {"asset_id": asset_id, "file": str(path.relative_to(root)),
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "bytes": path.stat().st_size}
            job = {"assets": [make(first, "asset_001"), make(second, "asset_002")]}
            bindings, problems = resolve_assets(root, job, project)
            self.assertEqual(bindings, {"4": "asset_002"})
            self.assertEqual(problems, [])

    def test_canonical_replay_reconstructs_exact_state_hash(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "raw.jsonl"
            baseline = {"timeline_id": "timeline", "duration_frames": 0, "tracks": [], "clips": [],
                        "compositions": [], "mixes": [], "master_effects": [{"native_id": 0, "effects": []}]}
            after = dict(baseline)
            after["duration_frames"] = 25
            after["clips"] = [{"native_id": 8, "asset_reference": "4", "track_native_id": 3,
                               "timeline_start_frame": 0, "duration_frames": 25}]
            events = [
                {"event_type": "state.checkpoint", "snapshot": baseline, "state_hash": canonical_hash(baseline)},
                {"event_type": "state.diff", "boundary": "commit", "event_id": "event-1",
                 "after_hash": canonical_hash(after), "diff": {"duration_after": 25, "changes": [
                    {"entity": "clip", "native_id": 8, "change": "added", "after": after["clips"][0]}]}},
            ]
            path.write_text("".join(json.dumps(event) + "\n" for event in events))
            report = replay_report([path])
            self.assertEqual(report["canonical_state_replay"], "passed")
            self.assertEqual(report["reconstructed_render"], "not_implemented")


if __name__ == "__main__":
    unittest.main()
