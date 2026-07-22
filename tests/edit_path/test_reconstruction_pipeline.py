# SPDX-FileCopyrightText: 2026 Edit Path contributors
# SPDX-License-Identifier: GPL-3.0-only

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from edit_path.io import sha256_file, write_json, write_jsonl
from edit_path.pipeline import (
    build_dataset_index,
    build_qa_queue,
    ingest_session,
    process_next_queued,
    process_session,
    publish_bundle,
    record_qa_review,
    preflight_session,
    validate_event_envelope,
    validate_state_transitions,
)
from edit_path.reconstruct import render_project
from edit_path.runtime import runtime_fingerprint
from edit_path.state import canonical_hash, load_state_reference, resolve_accepted_branch


def event(sequence: int, event_type: str, **values: object) -> dict:
    return {
        "schema_version": "0.3.0",
        "session_id": "session-test",
        "sequence": sequence,
        "event_id": f"event-{sequence}",
        "timestamp_utc": f"2026-07-22T00:00:{sequence:02d}Z",
        "event_type": event_type,
        **values,
    }


class BranchResolutionTests(unittest.TestCase):
    def test_merged_transaction_is_undone_and_redone_as_one_group(self) -> None:
        p0, p1, p2 = (character * 64 for character in "abc")
        checkpoint = event(1, "state.checkpoint", state_hash="d" * 64, snapshot={}, project_state={"sha256": p0})
        first = event(2, "state.diff", boundary="commit", transaction_id="tx", undo_entry_id="entry", project_before_hash=p0, project_after_hash=p1)
        merged = event(3, "state.diff", boundary="commit", transaction_id="tx", undo_entry_id="entry", project_before_hash=p1, project_after_hash=p2)
        undo = event(4, "state.diff", boundary="undo", transaction_id="undo", undo_entry_id="entry", target_transaction_id="tx", project_before_hash=p2, project_after_hash=p0)
        redo = event(5, "state.diff", boundary="redo", transaction_id="redo", undo_entry_id="entry", target_transaction_id="tx", project_before_hash=p0, project_after_hash=p2)
        self.assertEqual(resolve_accepted_branch([checkpoint, first, merged, undo], require_targets=True).accepted, [])
        self.assertEqual(resolve_accepted_branch([checkpoint, first, merged, undo, redo], require_targets=True).accepted, [first, merged])

    def test_wrong_undo_target_is_rejected(self) -> None:
        p0, p1, p0_again = (character * 64 for character in "aba")
        checkpoint = event(1, "state.checkpoint", state_hash="d" * 64, snapshot={}, project_state={"sha256": p0})
        commit = event(2, "state.diff", boundary="commit", transaction_id="tx", undo_entry_id="entry", project_before_hash=p0, project_after_hash=p1)
        undo = event(3, "state.diff", boundary="undo", transaction_id="undo", undo_entry_id="entry", target_transaction_id="other", project_before_hash=p1, project_after_hash=p0_again)
        with self.assertRaisesRegex(Exception, "targets a transaction other"):
            resolve_accepted_branch([checkpoint, commit, undo], require_targets=True)


class StateTests(unittest.TestCase):
    def test_multiple_timeline_hash_chains_are_validated_independently(self) -> None:
        empty_a = {"timeline_id": "a", "duration_frames": 0, "tracks": [], "clips": [], "compositions": [], "mixes": [], "master_effects": []}
        empty_b = {**empty_a, "timeline_id": "b"}
        events = [
            event(1, "state.checkpoint", timeline_id="a", label="a", snapshot=empty_a, state_hash=canonical_hash(empty_a)),
            event(2, "state.checkpoint", timeline_id="b", label="b", snapshot=empty_b, state_hash=canonical_hash(empty_b)),
        ]
        snapshots, states = validate_state_transitions(events)
        self.assertEqual(set(snapshots), {"a", "b"})
        self.assertEqual(len(states), 2)

    def test_zstd_sidecar_hash_and_size_are_checked(self) -> None:
        try:
            import zstandard
        except ImportError:
            self.skipTest("zstandard is not installed")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            raw = b"<mlt/>"
            encoded = zstandard.ZstdCompressor().compress(raw)
            (root / "state.zst").write_bytes(encoded)
            reference = {"encoding": "zstd", "path": "state.zst", "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}
            self.assertEqual(load_state_reference(reference, root), raw)


class RuntimeTests(unittest.TestCase):
    @mock.patch("edit_path.runtime.shutil.which", side_effect=lambda name: f"/usr/bin/{name}")
    @mock.patch("edit_path.runtime.subprocess.run")
    def test_ffmpeg_tools_use_the_supported_version_flag(self, run: mock.Mock, _which: mock.Mock) -> None:
        run.return_value = subprocess.CompletedProcess([], 0, stdout="tool version 1\n", stderr="")
        runtime_fingerprint()
        commands = [call.args[0] for call in run.call_args_list]
        self.assertIn(["/usr/bin/melt", "--version"], commands)
        self.assertIn(["/usr/bin/ffmpeg", "-version"], commands)
        self.assertIn(["/usr/bin/ffprobe", "-version"], commands)


class PublicationTests(unittest.TestCase):
    def test_no_edit_session_is_quarantined_with_a_specific_gate(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "session"
            output = Path(temporary) / "dataset"
            (root / "evidence").mkdir(parents=True)
            start = event(1, "session.start")
            end = event(2, "session.end")
            start["schema_version"] = end["schema_version"] = "0.2.0"
            write_jsonl(root / "evidence" / "raw-events.jsonl", [start, end])
            result = process_session(root, output)
            self.assertEqual(result["status"], "quarantined")
            self.assertEqual(result["gate"], "semantic_activity")
            self.assertTrue((Path(result["path"]) / "rejection.json").is_file())

    def test_atomic_bundle_contains_assets_and_portable_project(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "session"
            work = Path(temporary) / "work"
            output = Path(temporary) / "dataset" / "accepted"
            (root / "assets").mkdir(parents=True)
            work.mkdir()
            asset = root / "assets" / "source.mp4"
            asset.write_bytes(b"asset")
            manifest = {"schema": "video-path/assets@2", "assets": [{"asset_id": "asset_001", "original_filename": "source.mp4", "file": "assets/source.mp4", "sha256": sha256_file(asset), "bytes": 5, "license_status": "pending"}]}
            write_json(root / "asset-manifest.json", manifest)
            project = work / "reconstructed.kdenlive"
            project.write_text(f'<mlt><producer><property name="resource">{asset}</property></producer></mlt>', encoding="utf-8")
            final = work / "final.mp4"
            final.write_bytes(b"video")
            report = work / "report.json"
            write_json(report, {"ok": True})
            raw = root / "trajectory.jsonl"
            events = [event(1, "session.start"), event(2, "session.end", state_sidecars_complete=True)]
            write_jsonl(raw, events)
            bundle = publish_bundle(root, output, "session-test", {"final_video": final, "project": project, "report": report, "raw_trajectory": raw, "manifest_path": root / "asset-manifest.json"}, events, [], manifest)
            self.assertTrue((bundle / "assets" / "source.mp4").is_file())
            self.assertIn("assets/source.mp4", (bundle / "reconstructed.kdenlive").read_text(encoding="utf-8"))
            self.assertTrue((bundle / "bundle-manifest.json").is_file())


class PilotRegressionTests(unittest.TestCase):
    @unittest.skipUnless(Path("/home/tripl/pilot-session-028.jsonl").is_file(), "pilot 028 fixture is not available")
    def test_pilot_028_fails_the_minimum_edit_gate(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "evidence").mkdir()
            shutil.copy2("/home/tripl/pilot-session-028.jsonl", root / "evidence" / "raw-events.jsonl")
            with self.assertRaisesRegex(Exception, "no committed semantic edit"):
                preflight_session(root)

    @unittest.skipUnless(Path("/home/tripl/pilot-session-029.jsonl").is_file(), "pilot 029 fixture is not available")
    def test_pilot_029_is_rejected_as_incomplete(self) -> None:
        events = [json.loads(line) for line in Path("/home/tripl/pilot-session-029.jsonl").read_text(encoding="utf-8").splitlines() if line]
        with self.assertRaisesRegex(Exception, "incomplete"):
            validate_event_envelope(events)


@unittest.skipUnless(shutil.which("ffmpeg") and (shutil.which("melt") or shutil.which("mlt-melt")), "FFmpeg/MLT not installed")
class MediaIntegrationTests(unittest.TestCase):
    def test_real_checkpoint_and_final_ssim_pipeline(self) -> None:
        try:
            import zstandard
        except ImportError:
            self.skipTest("zstandard is not installed")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "session"
            (root / "assets").mkdir(parents=True)
            (root / "states").mkdir()
            (root / "checkpoint_refs").mkdir()
            (root / "output").mkdir()
            source = root / "assets" / "source.mp4"
            subprocess.run([shutil.which("ffmpeg") or "ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=25:duration=1", "-pix_fmt", "yuv420p", str(source)], check=True)

            def project(resource: str, service: str) -> bytes:
                return f'''<?xml version="1.0" encoding="utf-8"?>
<mlt LC_NUMERIC="C" root="{root}" producer="main">
 <profile frame_rate_num="25" frame_rate_den="1" width="320" height="180" progressive="1" sample_aspect_num="1" sample_aspect_den="1" display_aspect_num="16" display_aspect_den="9" colorspace="709"/>
 <producer id="clip" in="0" out="24"><property name="resource">{resource}</property><property name="mlt_service">{service}</property></producer>
 <playlist id="track"><entry producer="clip" in="0" out="24"/></playlist>
 <tractor id="main" in="0" out="24"><track producer="track"/></tractor>
</mlt>'''.encode()

            baseline_project = project("black", "color")
            final_project = project(str(source), "avformat")

            def state_ref(raw: bytes) -> dict:
                digest = hashlib.sha256(raw).hexdigest()
                relative = f"states/{digest}.kdenlive.zst"
                (root / relative).write_bytes(zstandard.ZstdCompressor().compress(raw))
                return {"encoding": "zstd", "path": relative, "base": "session", "sha256": digest, "bytes": len(raw), "durability": "complete_on_session_end"}

            baseline_ref, final_ref = state_ref(baseline_project), state_ref(final_project)
            melt = shutil.which("melt") or shutil.which("mlt-melt") or "melt"
            proxy_preset = {"crf": "28", "preset": "ultrafast", "ab": "64k", "width": "320", "height": "180", "rescale": "bilinear"}
            for name, raw in (("baseline", baseline_project), ("final", final_project)):
                project_path = root / f"{name}.kdenlive"
                project_path.write_bytes(raw)
                render_project(project_path, root / "checkpoint_refs" / f"{name}.mp4", melt_binary=melt, preset=proxy_preset)
            final_project_path = root / "final-source.kdenlive"
            final_project_path.write_bytes(final_project)
            render_project(final_project_path, root / "output" / "final.mp4", melt_binary=melt)

            empty = {"timeline_id": "timeline", "duration_frames": 25, "tracks": [], "clips": [], "compositions": [], "mixes": [], "master_effects": []}
            changed = {**empty, "clips": [{"native_id": 1, "entity_id": "clip-1", "asset_reference": "1", "asset_id": "asset_001", "track_native_id": 1, "timeline_start_frame": 0, "duration_frames": 25, "source_start_frame": 0, "source_end_frame": 24, "speed": 1.0, "effects": []}]}
            transaction = "transaction-1"
            entries = [
                event(1, "session.start"),
                event(2, "project.context", context={"project_id": "project", "fps_numerator": 25, "fps_denominator": 1, "width": 320, "height": 180, "sample_aspect_numerator": 1, "sample_aspect_denominator": 1, "display_aspect_numerator": 16, "display_aspect_denominator": 9, "colorspace": 709, "progressive": True, "bottom_field_first": False, "audio_channels": 2, "audio_sample_rate": None, "kdenlive_version": "test", "kdenlive_build": "test", "mlt_version": "7.38.0"}),
                event(3, "state.checkpoint", timeline_id="timeline", label="baseline", snapshot=empty, state_hash=canonical_hash(empty), project_state=baseline_ref, reference_proxy={"path": "checkpoint_refs/baseline.mp4", "base": "session", "width": 320, "height": 180, "render_preset": "checkpoint-proxy-v1"}),
                event(4, "action", action="clip.insert", timeline_id="timeline", parameters={}, transaction_id=transaction, undo_entry_id="entry-1"),
                event(5, "state.diff", timeline_id="timeline", label="insert", boundary="commit", transaction_id=transaction, undo_entry_id="entry-1", before_hash=canonical_hash(empty), after_hash=canonical_hash(changed), project_before_hash=baseline_ref["sha256"], project_after_hash=final_ref["sha256"], project_state=final_ref, diff={"changes": [{"entity": "clip", "native_id": 1, "change": "added", "after": changed["clips"][0]}]}),
                event(6, "state.checkpoint", timeline_id="timeline", label="final", snapshot=changed, state_hash=canonical_hash(changed), project_state=final_ref, reference_proxy={"path": "checkpoint_refs/final.mp4", "base": "session", "width": 320, "height": 180, "render_preset": "checkpoint-proxy-v1"}),
                event(7, "session.end", reason="test", state_sidecars_complete=True),
            ]
            write_jsonl(root / "trajectory.jsonl", entries)
            manifest = {"schema": "video-path/assets@2", "assets": [{"asset_id": "asset_001", "bin_reference": "1", "original_filename": "source.mp4", "file": "assets/source.mp4", "sha256": sha256_file(source), "bytes": source.stat().st_size, "license_status": "pending"}]}
            write_json(root / "asset-manifest.json", manifest)
            queue_root = Path(temporary) / "queue"
            dataset_root = Path(temporary) / "dataset"
            ingested = ingest_session(root, queue_root)
            self.assertEqual(ingested["status"], "queued", ingested)
            result = process_next_queued(queue_root, dataset_root, melt_binary=melt)
            self.assertEqual(result["status"], "accepted", result)
            bundle = Path(result["path"])
            self.assertTrue((bundle / "final.mp4").is_file())
            self.assertTrue((bundle / "reconstructed.kdenlive").is_file())
            self.assertTrue((bundle / "trajectory.jsonl").is_file())
            self.assertEqual(len(build_qa_queue(dataset_root, sample_rate=1)["samples"]), 1)
            record_qa_review(dataset_root, "session-test", reviewer="reviewer", status="rejected", notes="implausible edit")
            index = build_dataset_index(dataset_root)
            self.assertEqual(index["samples"], [])
            self.assertEqual(index["excluded"][0]["session_id"], "session-test")


if __name__ == "__main__":
    unittest.main()
