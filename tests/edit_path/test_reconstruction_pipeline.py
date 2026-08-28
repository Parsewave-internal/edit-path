# SPDX-FileCopyrightText: 2026 Edit Path contributors
# SPDX-License-Identifier: GPL-3.0-only

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from edit_path.io import replace_with_retry, sha256_file, write_json, write_jsonl
from edit_path.errors import EditPathError, GateError
from edit_path.pipeline import (
    attribution_coverage,
    build_dataset_index,
    build_qa_queue,
    ingest_session,
    process_next_queued,
    process_session,
    publish_bundle,
    quarantine_session,
    record_qa_review,
    preflight_session,
    validate_checkpoints,
    validate_event_envelope,
    validate_state_transitions,
)
from edit_path.process_video import (
    _parse_drawtext_filter,
    _write_ass_labels,
    build_replay_moments,
    build_replay_steps,
    render_edit_process,
)
from edit_path.reconstruct import render_project, select_video_encoder
from edit_path.runtime import runtime_fingerprint
from edit_path.state import (
    canonical_hash,
    effect_operation_name,
    load_state_reference,
    operation_name,
    resolve_accepted_branch,
    validate_action_semantics,
)

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "video-path-pilot"))
from job_pipeline import finalize_session


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
    def test_action_for_undone_transaction_is_ignored(self) -> None:
        deleted = event(2, "state.diff", boundary="commit", transaction_id="delete", diff={"changes": []})
        action = event(3, "action", action="clip.delete", transaction_id="delete")

        reports = validate_action_semantics([deleted, action], [])

        self.assertEqual(reports[0]["status"], "ignored")
        self.assertEqual(reports[0]["attribution"], "not_on_accepted_branch")

    def test_inconsistent_action_is_reported_as_degraded_instead_of_raising(self) -> None:
        moved = event(
            2,
            "state.diff",
            boundary="commit",
            transaction_id="move",
            diff={
                "changes": [
                    {
                        "entity": "clip",
                        "native_id": 1,
                        "change": "updated",
                        "before": {"timeline_start_frame": 0},
                        "after": {"timeline_start_frame": 10},
                    }
                ]
            },
        )
        action = event(3, "action", action="clip.delete", transaction_id="move")

        reports = validate_action_semantics([moved, action], [moved])

        self.assertEqual(reports[0]["inferred"], "clip.move")
        self.assertFalse(reports[0]["compatible"])
        self.assertEqual(reports[0]["status"], "degraded")

    def test_merged_transaction_is_undone_and_redone_as_one_group(self) -> None:
        p0, p1, p2 = (character * 64 for character in "abc")
        checkpoint = event(1, "state.checkpoint", state_hash="d" * 64, snapshot={}, project_state={"sha256": p0})
        first = event(2, "state.diff", boundary="commit", transaction_id="tx", undo_entry_id="entry", project_before_hash=p0, project_after_hash=p1)
        merged = event(3, "state.diff", boundary="commit", transaction_id="tx", undo_entry_id="entry", project_before_hash=p1, project_after_hash=p2)
        undo = event(4, "state.diff", boundary="undo", transaction_id="undo", undo_entry_id="entry", target_transaction_id="tx", project_before_hash=p2, project_after_hash=p0)
        redo = event(5, "state.diff", boundary="redo", transaction_id="redo", undo_entry_id="entry", target_transaction_id="tx", project_before_hash=p0, project_after_hash=p2)
        self.assertEqual(resolve_accepted_branch([checkpoint, first, merged, undo], require_targets=True).accepted, [])
        self.assertEqual(resolve_accepted_branch([checkpoint, first, merged, undo, redo], require_targets=True).accepted, [first, merged])

    def test_undo_reserializing_the_project_still_has_to_restore_the_timeline(self) -> None:
        """Observed on a real session: 6/6 undos restored the semantic snapshot
        exactly while 3/6 rewrote incidental project XML. The gate has to accept
        the reserialization and still reject a genuinely wrong restore."""
        p0, p1, p0_rewritten = ("a" * 64, "b" * 64, "c" * 64)
        s0, s1 = ("d" * 64, "e" * 64)
        checkpoint = event(1, "state.checkpoint", state_hash=s0, snapshot={}, project_state={"sha256": p0})
        commit = event(2, "state.diff", boundary="commit", transaction_id="tx", undo_entry_id="entry",
                       project_before_hash=p0, project_after_hash=p1, before_hash=s0, after_hash=s1)

        # Project bytes differ after the undo, timeline is restored: accepted.
        undo = event(3, "state.diff", boundary="undo", transaction_id="undo", undo_entry_id="entry",
                     target_transaction_id="tx", project_before_hash=p1, project_after_hash=p0_rewritten,
                     before_hash=s1, after_hash=s0)
        resolution = resolve_accepted_branch([checkpoint, commit, undo], require_targets=True)
        self.assertEqual(resolution.accepted, [])

        # Timeline not restored: still rejected, so the gate keeps its teeth.
        wrong = event(3, "state.diff", boundary="undo", transaction_id="undo", undo_entry_id="entry",
                      target_transaction_id="tx", project_before_hash=p1, project_after_hash=p0,
                      before_hash=s1, after_hash="f" * 64)
        with self.assertRaisesRegex(GateError, "undo should restore semantic state"):
            resolve_accepted_branch([checkpoint, commit, wrong], require_targets=True)

    def test_wrong_undo_target_is_rejected(self) -> None:
        p0, p1, p0_again = (character * 64 for character in "aba")
        checkpoint = event(1, "state.checkpoint", state_hash="d" * 64, snapshot={}, project_state={"sha256": p0})
        commit = event(2, "state.diff", boundary="commit", transaction_id="tx", undo_entry_id="entry", project_before_hash=p0, project_after_hash=p1)
        undo = event(3, "state.diff", boundary="undo", transaction_id="undo", undo_entry_id="entry", target_transaction_id="other", project_before_hash=p1, project_after_hash=p0_again)
        with self.assertRaisesRegex(Exception, "targets a transaction other"):
            resolve_accepted_branch([checkpoint, commit, undo], require_targets=True)

    def test_post_push_action_is_rebound_to_the_matching_previous_diff(self) -> None:
        checkpoint = event(1, "state.checkpoint", state_hash="d" * 64, snapshot={}, project_state={"sha256": "a" * 64})
        inserted = event(2, "state.diff", boundary="commit", transaction_id="insert", undo_entry_id="insert-entry",
                         project_before_hash="a" * 64, project_after_hash="b" * 64,
                         diff={"changes": [{"entity": "clip", "native_id": 1, "change": "added", "after": {}}]})
        late_action = event(3, "action", action="clip.insert", transaction_id="move", timeline_id="timeline", parameters={})
        moved = event(4, "state.diff", boundary="commit", transaction_id="move", undo_entry_id="move-entry",
                      project_before_hash="b" * 64, project_after_hash="c" * 64,
                      diff={"changes": [{"entity": "clip", "native_id": 1, "change": "updated",
                                          "before": {"timeline_start_frame": 0}, "after": {"timeline_start_frame": 10}}]})
        accepted = resolve_accepted_branch([checkpoint, inserted, late_action, moved], require_targets=True).accepted
        reports = validate_action_semantics([checkpoint, inserted, late_action, moved], accepted)
        self.assertEqual(reports[0]["declared"], "clip.insert")
        self.assertEqual(reports[0]["attribution"], "recovered_post_push")
        self.assertIsNone(reports[1]["declared"])


class StateTests(unittest.TestCase):
    def test_exact_preview_failure_uses_semantic_state_and_keeps_replay(self) -> None:
        moment = {
            "index": 0,
            "sequence": 2,
            "event": {"event_id": "diff", "event_type": "state.diff"},
            "event_type": "state.diff",
            "operation": "clip.insert",
            "snapshot": {
                "duration_frames": 25,
                "clips": [{"native_id": 1, "timeline_start_frame": 0}],
                "compositions": [],
            },
            "state_event": {"project_state": {"sha256": "b" * 64, "path": "states/missing.zst"}},
        }

        def fake_scene(*args: object, **_kwargs: object) -> int:
            Path(args[3]).write_bytes(b"scene")
            return 10

        def fake_run(command: list[str], _label: str, **_kwargs: object) -> None:
            Path(command[-1]).write_bytes(b"replay")

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with (
                mock.patch("edit_path.process_video.shutil.which", return_value="ffmpeg"),
                mock.patch("edit_path.process_video.select_video_encoder", return_value="libx264"),
                mock.patch("edit_path.process_video._supports_filter", return_value=True),
                mock.patch("edit_path.process_video.build_replay_steps", return_value=[]),
                mock.patch("edit_path.process_video.build_replay_moments", return_value=[moment]),
                mock.patch("edit_path.process_video._asset_lookup", return_value=({}, [])),
                mock.patch("edit_path.process_video.render_event", side_effect=EditPathError("unmanifested asset")),
                mock.patch("edit_path.process_video._render_scene", side_effect=fake_scene),
                mock.patch("edit_path.process_video._run", side_effect=fake_run),
                mock.patch(
                    "edit_path.process_video.probe",
                    return_value={"format": {"duration": "1.5"}, "streams": [{"codec_type": "video"}]},
                ),
            ):
                output, report = render_edit_process(
                    root,
                    [],
                    [],
                    "a" * 64,
                    root / "replay.mp4",
                    root / "work",
                )
                output_exists = output.is_file()

        self.assertTrue(output_exists)
        self.assertEqual(report["state_preview_quality"], "degraded")
        self.assertEqual(report["events"][0]["monitor"], "semantic_ui_only")
        self.assertIn("semantic editor state used instead", report["state_preview_warnings"][0])

    def test_replay_label_failure_falls_back_to_degraded_unlabeled_video(self) -> None:
        moment = {
            "index": 0,
            "sequence": 1,
            "event": {"event_id": "start", "event_type": "session.start"},
            "event_type": "session.start",
            "operation": "session.start",
            "snapshot": {"duration_frames": 0, "clips": [], "compositions": []},
            "state_event": None,
        }
        renderers: list[str] = []

        def fake_scene(*_args: object, **kwargs: object) -> int:
            renderer = str(kwargs["text_renderer"])
            renderers.append(renderer)
            if renderer == "ass":
                raise EditPathError("simulated ASS renderer failure")
            Path(_args[3]).write_bytes(b"scene")
            return 0

        def fake_run(command: list[str], _label: str, **_kwargs: object) -> None:
            Path(command[-1]).write_bytes(b"replay")

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with (
                mock.patch("edit_path.process_video.shutil.which", return_value="ffmpeg"),
                mock.patch("edit_path.process_video.select_video_encoder", return_value="libx264"),
                mock.patch("edit_path.process_video._supports_filter", side_effect=[False, True]),
                mock.patch("edit_path.process_video.build_replay_steps", return_value=[]),
                mock.patch("edit_path.process_video.build_replay_moments", return_value=[moment]),
                mock.patch("edit_path.process_video._asset_lookup", return_value=({}, [])),
                mock.patch("edit_path.process_video._render_scene", side_effect=fake_scene),
                mock.patch("edit_path.process_video._run", side_effect=fake_run),
                mock.patch(
                    "edit_path.process_video.probe",
                    return_value={"format": {"duration": "2.0"}, "streams": [{"codec_type": "video"}]},
                ),
            ):
                output, report = render_edit_process(
                    root,
                    [],
                    [],
                    "a" * 64,
                    root / "replay.mp4",
                    root / "work",
                )
                output_exists = output.is_file()

        self.assertTrue(output_exists)
        self.assertEqual(renderers, ["ass", "none"])
        self.assertEqual(report["training_ui_quality"], "degraded")
        self.assertEqual(report["text_overlays"], "none")
        self.assertIn("replay continued without labels", report["training_ui_warnings"][0])

    def test_replay_converts_required_labels_to_ass_when_drawtext_is_unavailable(self) -> None:
        label = _parse_drawtext_filter(
            "drawtext=font=Sans:text='Project Bin':x=18:y=94:fontsize=18:fontcolor=0xf3f4f6"
        )
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "labels.ass"
            _write_ass_labels(path, [label], 1.35)
            content = path.read_text(encoding="utf-8")

        self.assertIn("Project Bin", content)
        self.assertIn(r"\pos(18,94)", content)
        self.assertIn("PlayResX: 1920", content)

    def test_edit_process_replays_baseline_then_every_accepted_state(self) -> None:
        empty = {
            "timeline_id": "timeline",
            "duration_frames": 0,
            "tracks": [],
            "clips": [],
            "compositions": [],
            "mixes": [],
            "master_effects": [],
        }
        changed = {
            **empty,
            "duration_frames": 25,
            "clips": [{"native_id": 1, "timeline_start_frame": 0, "duration_frames": 25}],
        }
        baseline_hash, final_hash = "a" * 64, "b" * 64
        checkpoint = event(
            1,
            "state.checkpoint",
            timeline_id="timeline",
            snapshot=empty,
            state_hash=canonical_hash(empty),
            project_state={"sha256": baseline_hash},
        )
        commit = event(
            2,
            "state.diff",
            timeline_id="timeline",
            label="Insert Clip",
            boundary="commit",
            transaction_id="transaction",
            undo_entry_id="entry",
            before_hash=canonical_hash(empty),
            after_hash=canonical_hash(changed),
            project_before_hash=baseline_hash,
            project_after_hash=final_hash,
            project_state={"sha256": final_hash},
            diff={
                "duration_before": 0,
                "duration_after": 25,
                "changes": [
                    {
                        "entity": "clip",
                        "native_id": 1,
                        "change": "added",
                        "after": changed["clips"][0],
                    }
                ],
            },
        )
        steps = build_replay_steps([checkpoint, commit], [commit], baseline_hash)
        self.assertEqual([step["operation"] for step in steps], ["timeline.initial_state", "clip.insert"])
        self.assertEqual(steps[1]["snapshot"]["duration_frames"], 25)
        self.assertEqual(steps[1]["project_hash"], final_hash)

        shortcut = event(3, "ui.shortcut", key_sequence="Ctrl+V", focus="TimelineWidget")
        gesture = event(
            4,
            "ui.gesture",
            gesture="drag",
            interaction_id="drag-1",
            start_global={"x": 800, "y": 650},
            end_global={"x": 700, "y": 650},
        )
        commit["sequence"] = 5
        commit["event_id"] = "event-5"
        commit["interaction_id"] = "drag-1"
        moments = build_replay_moments([checkpoint, shortcut, gesture, commit])
        self.assertEqual([moment["event_type"] for moment in moments], ["state.checkpoint", "ui.shortcut", "ui.gesture", "state.diff"])
        self.assertEqual(moments[1]["operation"], "edit.paste")
        self.assertEqual(moments[2]["operation"], "clip.insert")

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
    @mock.patch("edit_path.reconstruct.shutil.which", return_value="/usr/bin/ffmpeg")
    @mock.patch("edit_path.reconstruct.subprocess.run")
    def test_encoder_selection_falls_back_to_openh264(self, run: mock.Mock, _which: mock.Mock) -> None:
        run.return_value = subprocess.CompletedProcess(
            [],
            0,
            stdout=" V....D libopenh264 OpenH264 encoder\n V.S... mpeg4 MPEG-4 encoder\n",
            stderr="",
        )
        self.assertEqual(select_video_encoder(), "libopenh264")

    @mock.patch("edit_path.validate.probe")
    @mock.patch("edit_path.reconstruct.available_video_encoders", return_value={"libx264", "libopenh264"})
    @mock.patch("edit_path.reconstruct.subprocess.run")
    def test_audio_only_melt_success_retries_the_next_encoder(
        self,
        run: mock.Mock,
        _encoders: mock.Mock,
        probe_media: mock.Mock,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = root / "project.kdenlive"
            output = root / "output.mp4"
            project.write_text("<mlt/>", encoding="utf-8")

            def render(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess:
                target = Path(next(value for value in command if value.startswith("avformat:")).removeprefix("avformat:"))
                target.write_bytes(b"media")
                return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

            run.side_effect = render
            probe_media.side_effect = [
                {"streams": [{"codec_type": "audio"}]},
                {"streams": [{"codec_type": "video"}, {"codec_type": "audio"}]},
            ]
            self.assertEqual(render_project(project, output, melt_binary="/usr/bin/melt"), output)
            self.assertTrue(output.is_file())
            commands = [call.args[0] for call in run.call_args_list]
            self.assertTrue(any("vcodec=libx264" in command for command in commands))
            self.assertTrue(any("vcodec=libopenh264" in command for command in commands))
            self.assertTrue(all("real_time=-1" in command for command in commands))

    @mock.patch("edit_path.runtime.shutil.which", side_effect=lambda name: f"/usr/bin/{name}")
    @mock.patch("edit_path.runtime.subprocess.run")
    def test_ffmpeg_tools_use_the_supported_version_flag(self, run: mock.Mock, _which: mock.Mock) -> None:
        run.return_value = subprocess.CompletedProcess([], 0, stdout="tool version 1\n", stderr="")
        runtime_fingerprint()
        commands = [call.args[0] for call in run.call_args_list]
        self.assertIn(["/usr/bin/melt", "--version"], commands)
        self.assertIn(["/usr/bin/ffmpeg", "-version"], commands)
        self.assertIn(["/usr/bin/ffprobe", "-version"], commands)


class AtomicReplaceTests(unittest.TestCase):
    def test_repeated_quarantine_uses_a_unique_destination(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            trajectory = root / "trajectory.jsonl"
            trajectory.write_text("evidence\n", encoding="utf-8")
            quarantine = root / "quarantine"
            suffix = sha256_file(trajectory)[:12]
            (quarantine / "session").mkdir(parents=True)
            (quarantine / f"session-{suffix}").mkdir()

            destination = quarantine_session(
                root,
                quarantine,
                "session",
                GateError("test", "failure"),
                trajectory,
            )

            self.assertEqual(destination.name, f"session-{suffix}-2")
            self.assertTrue((destination / "rejection.json").is_file())

    @mock.patch("edit_path.io.time.sleep")
    @mock.patch("edit_path.io.os.replace")
    def test_transient_access_denied_is_retried(self, replace: mock.Mock, sleep: mock.Mock) -> None:
        replace.side_effect = [PermissionError(13, "Access is denied"), None]

        replace_with_retry(Path("temporary"), Path("published"))

        self.assertEqual(replace.call_count, 2)
        sleep.assert_called_once_with(0.05)

    @mock.patch("edit_path.io.time.sleep")
    @mock.patch("edit_path.io.os.replace", side_effect=FileNotFoundError("missing"))
    def test_unrelated_replace_error_is_not_retried(self, replace: mock.Mock, sleep: mock.Mock) -> None:
        with self.assertRaises(FileNotFoundError):
            replace_with_retry(Path("missing"), Path("published"))

        replace.assert_called_once()
        sleep.assert_not_called()


class PublicationTests(unittest.TestCase):
    def test_unmanifested_reconstruction_asset_falls_back_to_editor_project(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "session"
            work = Path(temporary) / "work"
            output = Path(temporary) / "dataset" / "accepted"
            (root / "assets").mkdir(parents=True)
            (root / "internal").mkdir()
            work.mkdir()
            asset = root / "assets" / "source.mp4"
            asset.write_bytes(b"asset")
            unmanifested = root / "other-source.mp4"
            unmanifested.write_bytes(b"other")
            manifest = {
                "schema": "video-path/assets@2",
                "assets": [
                    {
                        "asset_id": "asset_001",
                        "original_filename": "source.mp4",
                        "original_path": str(asset),
                        "file": "assets/source.mp4",
                        "sha256": sha256_file(asset),
                        "bytes": asset.stat().st_size,
                        "license_status": "pending",
                    }
                ],
            }
            write_json(root / "asset-manifest.json", manifest)
            project = work / "reconstructed.kdenlive"
            project.write_text(f'<mlt><producer><property name="resource">{unmanifested}</property></producer></mlt>', encoding="utf-8")
            (root / "internal" / "final.kdenlive").write_text(
                f'<mlt><producer><property name="resource">{asset}</property></producer></mlt>',
                encoding="utf-8",
            )
            final = work / "final.mp4"
            final.write_bytes(b"video")
            report = work / "report.json"
            write_json(report, {"quality_status": "passed", "quality_warnings": []})
            raw = root / "trajectory.jsonl"
            events = [event(1, "session.start"), event(2, "session.end", state_sidecars_complete=True)]
            write_jsonl(raw, events)

            bundle = publish_bundle(
                root,
                output,
                "session-test",
                {
                    "final_video": final,
                    "project": project,
                    "report": report,
                    "raw_trajectory": raw,
                    "manifest_path": root / "asset-manifest.json",
                },
                events,
                [],
                manifest,
            )

            portable = (bundle / "reconstructed.kdenlive").read_text(encoding="utf-8")
            published_report = json.loads((bundle / "render-report.json").read_text(encoding="utf-8"))
            self.assertIn("assets/source.mp4", portable)
            self.assertNotIn("other-source.mp4", portable)
            self.assertEqual(published_report["quality_status"], "degraded")
            self.assertEqual(published_report["quality_warnings"][0]["fallback"], "used_editor_saved_project")

    def test_missing_checkpoint_proxy_is_degraded_instead_of_rejected(self) -> None:
        checkpoint = event(
            1,
            "state.checkpoint",
            project_state={"sha256": "a" * 64},
            snapshot={"duration_frames": 25, "clips": [{"native_id": 1}], "compositions": [], "mixes": []},
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            results = validate_checkpoints(
                root,
                [checkpoint],
                root / "work",
                minimum_ssim=0.995,
                melt_binary=None,
                require_references=True,
            )

        self.assertEqual(results[0]["status"], "degraded")
        self.assertFalse(results[0]["accepted"])

    def test_final_ssim_miss_packages_with_state_replay_and_warning(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            session = root / "session"
            output_root = root / "pipeline-output"
            session.mkdir()
            output_root.mkdir()
            trajectory = session / "trajectory.jsonl"
            trajectory.write_text("", encoding="utf-8")
            reference = session / "editor-final.mp4"
            reference.write_bytes(b"editor-final")
            manifest_path = session / "asset-manifest.json"
            write_json(manifest_path, {"schema": "video-path/assets@2", "assets": []})
            captured_report: dict[str, object] = {}

            def fake_render_session(_session: Path, destination: Path, **_kwargs: object) -> Path:
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(b"recreated")
                destination.with_suffix(".kdenlive").write_text("<mlt/>", encoding="utf-8")
                return destination

            def fake_replay(
                _session: Path,
                _events: list[dict],
                _accepted: list[dict],
                _baseline_hash: str,
                destination: Path,
                _work_dir: Path,
                **_kwargs: object,
            ) -> tuple[Path, dict]:
                destination.write_bytes(b"state-replay")
                return destination, {"accepted": True, "training_ui_quality": "passed"}

            def fake_publish(
                _session: Path,
                destination_root: Path,
                session_id: str,
                artifacts: dict,
                *_args: object,
            ) -> Path:
                captured_report.update(json.loads(Path(artifacts["report"]).read_text(encoding="utf-8")))
                destination = destination_root / session_id
                destination.mkdir(parents=True)
                return destination

            preflight = {
                "trajectory": trajectory,
                "events": [],
                "envelope": {"session_id": "session-test"},
                "require_exact": False,
                "state_reports": [],
                "project_states": 0,
                "stable_entities": 0,
                "branch": SimpleNamespace(accepted=[], baseline_hash="a" * 64, final_hash="b" * 64),
                "actions": [],
                "activity": {},
                "manifest_path": manifest_path,
                "manifest": {"schema": "video-path/assets@2", "assets": []},
                "verified_assets": [],
            }
            mismatch = {
                "accepted": False,
                "ssim": 0.75,
                "minimum_ssim": 0.99,
                "duration_delta_seconds": 0.0,
            }
            with (
                mock.patch("edit_path.pipeline.preflight_session", return_value=preflight),
                mock.patch("edit_path.pipeline.runtime_fingerprint", return_value={}),
                mock.patch("edit_path.pipeline.validate_checkpoints", return_value=[]),
                mock.patch("edit_path.pipeline._reference_video", return_value=reference),
                mock.patch("edit_path.pipeline.reference_matched_render", return_value=(".mp4", None, True)),
                mock.patch("edit_path.pipeline.render_session", side_effect=fake_render_session),
                mock.patch("edit_path.pipeline.validate_render", return_value=mismatch.copy()),
                mock.patch("edit_path.pipeline.render_edit_process", side_effect=fake_replay),
                mock.patch("edit_path.pipeline.publish_bundle", side_effect=fake_publish),
            ):
                result = process_session(session, output_root)

            self.assertEqual(result["status"], "accepted")
            self.assertEqual(captured_report["quality_status"], "degraded")
            self.assertEqual(captured_report["final"]["status"], "degraded")
            self.assertEqual(captured_report["quality_warnings"][0]["gate"], "final_render")
            self.assertEqual(
                captured_report["quality_warnings"][0]["fallback"],
                "preserved_editor_final_and_generated_state_replay",
            )

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
            manifest = {"schema": "video-path/assets@2", "assets": [{"asset_id": "asset_001", "original_filename": "source.mp4", "original_path": "/private/source.mp4", "source": str(asset), "file": "assets/source.mp4", "sha256": sha256_file(asset), "bytes": 5, "license_status": "pending"}]}
            write_json(root / "asset-manifest.json", manifest)
            project = work / "reconstructed.kdenlive"
            project.write_text(f'<mlt><producer><property name="resource">{asset}</property></producer></mlt>', encoding="utf-8")
            final = work / "final.mp4"
            final.write_bytes(b"video")
            reconstructed = work / "reconstructed-output.mp4"
            reconstructed.write_bytes(b"reconstructed")
            report = work / "report.json"
            write_json(report, {"ok": True})
            raw = root / "trajectory.jsonl"
            events = [event(1, "session.start"), event(2, "session.end", state_sidecars_complete=True)]
            write_jsonl(raw, events)
            real_replace = os.replace
            blocked_once = False

            def transient_directory_lock(source: Path | str, destination: Path | str) -> None:
                nonlocal blocked_once
                if not blocked_once and Path(source).is_dir() and Path(destination).name == "session-test":
                    blocked_once = True
                    raise PermissionError(13, "Access is denied")
                real_replace(source, destination)

            with mock.patch("edit_path.io.os.replace", side_effect=transient_directory_lock), mock.patch(
                "edit_path.io.time.sleep"
            ) as sleep:
                bundle = publish_bundle(root, output, "session-test", {"final_video": final, "reconstructed_video": reconstructed, "project": project, "report": report, "raw_trajectory": raw, "manifest_path": root / "asset-manifest.json"}, events, [], manifest)
            self.assertTrue(blocked_once)
            sleep.assert_called_once_with(0.05)
            self.assertEqual(stat.S_IMODE(bundle.stat().st_mode), 0o755)
            self.assertTrue((bundle / "assets" / "source.mp4").is_file())
            self.assertEqual((bundle / "reconstructed-output.mp4").read_bytes(), b"reconstructed")
            self.assertIn("assets/source.mp4", (bundle / "reconstructed.kdenlive").read_text(encoding="utf-8"))
            self.assertTrue((bundle / "bundle-manifest.json").is_file())
            published = json.loads((bundle / "asset-manifest.json").read_text(encoding="utf-8"))
            self.assertNotIn("source", published["assets"][0])
            self.assertNotIn("original_path", published["assets"][0])


class LongSessionLoadTests(unittest.TestCase):
    def test_five_hour_equivalent_event_volume_keeps_a_valid_envelope(self) -> None:
        events = [event(1, "session.start"), event(2, "project.context", context={
            "project_id": "project", "fps_numerator": 25, "fps_denominator": 1, "width": 1920, "height": 1080,
            "sample_aspect_numerator": 1, "sample_aspect_denominator": 1, "display_aspect_numerator": 16,
            "display_aspect_denominator": 9, "colorspace": 709, "progressive": True, "bottom_field_first": False,
            "audio_channels": 2, "audio_sample_rate": 48000, "kdenlive_version": "test", "kdenlive_build": "test",
            "mlt_version": "test",
        })]
        for sequence in range(3, 18_003):
            events.append(event(sequence, "ui.command", command_id="noop", interaction_id=f"interaction-{sequence}",
                                label="Load event", source="programmatic_or_unknown", shortcuts=[]))
        events.append(event(18_003, "session.end", state_sidecars_complete=True))
        envelope = validate_event_envelope(events)
        self.assertTrue(envelope["complete"])
        self.assertEqual(envelope["events"], 18_003)


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
 <producer id="clip" in="0" out="24"><property name="resource">{resource}</property><property name="mlt_service">{service}</property><property name="kdenlive:id">1</property></producer>
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
            write_json(root / "session.json", {"schema_version": "0.3.0", "status": "ready_to_finish"})
            queue_root = Path(temporary) / "queue"
            dataset_root = Path(temporary) / "dataset"
            ingested = ingest_session(root, queue_root)
            self.assertEqual(ingested["status"], "queued", ingested)
            result = process_next_queued(queue_root, dataset_root, melt_binary=melt)
            self.assertEqual(result["status"], "accepted", result)
            bundle = Path(result["path"])
            self.assertTrue((bundle / "final.mp4").is_file())
            self.assertTrue((bundle / "reconstructed-output.mp4").is_file())
            self.assertTrue((bundle / "reconstructed.kdenlive").is_file())
            self.assertTrue((bundle / "trajectory.jsonl").is_file())
            self.assertEqual(len(build_qa_queue(dataset_root, sample_rate=1)["samples"]), 1)
            record_qa_review(dataset_root, "session-test", reviewer="reviewer", status="rejected", notes="implausible edit")
            index = build_dataset_index(dataset_root)
            self.assertEqual(index["samples"], [])
            self.assertEqual(index["excluded"][0]["session_id"], "session-test")

            write_jsonl(root / "raw-events-001.jsonl", entries)
            completed = finalize_session(
                root,
                final_project_path,
                root / "output" / "final.mp4",
                {
                    "schema_version": "0.1.0",
                    "job_id": "session-test",
                    "task": {"prompt": None},
                    "project": {"frame_rate": {"numerator": 25, "denominator": 1}, "width": 320, "height": 180},
                },
            )
            self.assertTrue((completed / "edit-path" / "replay.mp4").is_file())
            self.assertTrue((completed / "verification" / "reconstructed.mp4").is_file())
            if sys.platform != "win32":
                self.assertEqual(stat.S_IMODE(completed.stat().st_mode), 0o755)
            self.assertTrue((completed / "outputs" / "final.mp4").is_file())
            self.assertTrue((completed / "provenance" / "segments" / "raw-events-001.jsonl").is_file())
            bindings = json.loads((completed / "provenance" / "asset-bindings.json").read_text(encoding="utf-8"))
            self.assertTrue(bindings["bindings"])
            sample = json.loads((completed / "sample.json").read_text(encoding="utf-8"))
            self.assertEqual(sample["task"]["prompt_status"], "pending_internal_entry")
            self.assertEqual(sample["quality"]["media_reconstruction"], "passed")
            self.assertEqual(sample["output"]["edit_process_video"], "edit-path/replay.mp4")
            self.assertEqual(sample["output"]["reconstructed_video"], "verification/reconstructed.mp4")
            self.assertEqual(json.loads((root / "session.json").read_text(encoding="utf-8"))["status"], "packaged")
            self.assertEqual(json.loads((completed / "session.json").read_text(encoding="utf-8"))["status"], "packaged")


class CommandCorrelationTests(unittest.TestCase):
    """Joining ui.command evidence to the state it produced.

    The recorder stamps interaction_id onto a diff only inside a two-second
    window after the input, but stamps transaction_id unconditionally. These
    pin the transaction join so command correlation stops depending on how fast
    the editor happened to be working.
    """

    CLIP = {"native_id": 1, "entity_id": "clip-1", "track_native_id": 2,
            "timeline_start_frame": 0, "duration_frames": 25, "effects": []}
    EMPTY = {"timeline_id": "tl", "duration_frames": 0, "tracks": [], "clips": [],
             "compositions": [], "mixes": [], "master_effects": []}

    def _snapshots(self) -> tuple[dict, dict]:
        before = {**self.EMPTY, "duration_frames": 25, "clips": [dict(self.CLIP)]}
        after = {**self.EMPTY, "duration_frames": 35,
                 "clips": [{**self.CLIP, "timeline_start_frame": 10}]}
        return before, after

    def _baseline(self) -> dict:
        before, _ = self._snapshots()
        return event(1, "state.checkpoint", timeline_id="tl", label="baseline",
                     snapshot=before, state_hash=canonical_hash(before))

    def _diff(self, sequence: int, **values: object) -> dict:
        """A real move, hash-chained onto the baseline so the gates accept it."""
        before, after = self._snapshots()
        return event(
            sequence,
            "state.diff",
            boundary="commit",
            timeline_id="tl",
            label="Move clip",
            before_hash=canonical_hash(before),
            after_hash=canonical_hash(after),
            diff={
                "changes": [
                    {
                        "entity": "clip",
                        "native_id": 1,
                        "change": "updated",
                        "before": before["clips"][0],
                        "after": after["clips"][0],
                    }
                ],
                "duration_before": 25,
                "duration_after": 35,
            },
            **values,
        )

    def test_command_links_to_its_diff_without_an_interaction_id(self) -> None:
        # The case the interaction index cannot serve: the editor paused, so the
        # recorder never stamped interaction_id onto the diff.
        command = event(2, "ui.command", interaction_id="i-1", command_id="edit_move",
                        label="Move", source="menu", shortcuts=[], transaction_id="tx-1")
        diff = self._diff(3, transaction_id="tx-1")

        moments = build_replay_moments([self._baseline(), command, diff])
        command_moment = next(m for m in moments if m["event_type"] == "ui.command")

        self.assertIsNotNone(command_moment["linked_diff"])
        self.assertEqual(command_moment["linked_diff"]["transaction_id"], "tx-1")

    def test_unmapped_command_is_named_by_the_state_it_produced(self) -> None:
        # command_id "unmapped" means the Qt action had no objectName. Falling
        # back to the diff is more informative than the visible label.
        command = event(2, "ui.command", interaction_id="i-1", command_id="unmapped",
                        label="Some Menu Item", source="menu", shortcuts=[], transaction_id="tx-1")
        diff = self._diff(3, transaction_id="tx-1")

        moments = build_replay_moments([self._baseline(), command, diff])
        command_moment = next(m for m in moments if m["event_type"] == "ui.command")

        self.assertEqual(command_moment["operation"], "clip.move")

    def test_unmapped_command_without_a_link_still_falls_back_to_its_label(self) -> None:
        command = event(2, "ui.command", interaction_id="i-1", command_id="unmapped",
                        label="Some Menu Item", source="menu", shortcuts=[])

        moments = build_replay_moments([self._baseline(), command])
        command_moment = next(m for m in moments if m["event_type"] == "ui.command")

        self.assertIsNone(command_moment["linked_diff"])
        self.assertEqual(command_moment["operation"], "command.some_menu_item")

    def test_interaction_id_still_wins_when_the_recorder_supplied_one(self) -> None:
        # The existing join must keep priority: it is the recorder's own
        # statement about causation, where the transaction join is an inference.
        command = event(2, "ui.command", interaction_id="i-1", command_id="edit_move",
                        label="Move", source="menu", shortcuts=[], transaction_id="tx-other")
        precise = self._diff(3, interaction_id="i-1", transaction_id="tx-precise")
        competing = self._diff(4, transaction_id="tx-other")

        # Two diffs cannot share one hash chain, and the chain is not what this
        # test is about, so the transition gate is isolated out.
        with mock.patch("edit_path.process_video.validate_state_transitions", return_value=(None, [])):
            moments = build_replay_moments([command, precise, competing])
        command_moment = next(m for m in moments if m["event_type"] == "ui.command")

        self.assertEqual(command_moment["linked_diff"]["transaction_id"], "tx-precise")

    def test_coverage_counts_unmapped_and_correlated_commands(self) -> None:
        diff = self._diff(2, transaction_id="tx-1")
        mapped = event(3, "ui.command", interaction_id="i-1", command_id="edit_move",
                       label="Move", source="menu", shortcuts=[], transaction_id="tx-1")
        unmapped = event(4, "ui.command", interaction_id="i-2", command_id="unmapped",
                         label="Mystery", source="menu", shortcuts=[])
        events = [self._baseline(), diff, mapped, unmapped]

        coverage = attribution_coverage(validate_action_semantics(events, [diff]), events)

        self.assertEqual(coverage["ui_commands"], 2)
        self.assertEqual(coverage["unmapped_commands"], 1)
        self.assertEqual(coverage["transaction_linked_commands"], 1)


class EffectAttributionTests(unittest.TestCase):
    """Effect and keyframe intent recovered from the diffs the recorder already writes.

    The fixtures mirror ``canonicalXmlElement`` in videopathrecorder.cpp: each
    effect is ``{"name": "effect", "attributes": {"id": ...}, "children": [...]}``
    and each parameter is a ``property`` child whose value lives in ``text``.
    """

    @staticmethod
    def _effect(identifier: str, **properties: str) -> dict:
        return {
            "name": "effect",
            "attributes": {"id": identifier},
            "children": [
                {"name": "property", "attributes": {"name": name}, "text": value}
                for name, value in properties.items()
            ],
        }

    def _clip_diff(self, before: list, after: list) -> dict:
        return {
            "changes": [
                {
                    "entity": "clip",
                    "native_id": 1,
                    "change": "updated",
                    "before": {"effects": before},
                    "after": {"effects": after},
                }
            ]
        }

    def test_stack_membership_changes_are_named(self) -> None:
        transform = self._effect("qtblend", rect="0 0 100 100")
        blur = self._effect("avfilter.gblur", sigma="5")
        self.assertEqual(effect_operation_name([], [transform]), "effect.add")
        self.assertEqual(effect_operation_name([transform], []), "effect.remove")
        self.assertEqual(effect_operation_name([transform, blur], [blur, transform]), "effect.reorder")
        self.assertIsNone(effect_operation_name([transform], [transform]))

    def test_keyframed_parameter_is_distinguished_from_a_static_one(self) -> None:
        # MLT writes animated values as "frame=value" pairs; that is the only
        # reliable signal, since parameter names differ per effect.
        static_before = self._effect("avfilter.gblur", sigma="5")
        static_after = self._effect("avfilter.gblur", sigma="8")
        self.assertEqual(
            operation_name(self._clip_diff([static_before], [static_after])),
            "effect.parameter_change",
        )
        keyed_before = self._effect("qtblend", rect="0=0 0 100 100")
        keyed_after = self._effect("qtblend", rect="0=0 0 100 100;50=10 10 80 80")
        self.assertEqual(
            operation_name(self._clip_diff([keyed_before], [keyed_after])),
            "effect.keyframe_change",
        )

    def test_repeated_effect_ids_are_told_apart_by_stack_position(self) -> None:
        # Kdenlive allows the same effect twice on one clip, so the id alone
        # cannot identify it; a change to the second copy must still register.
        first = self._effect("avfilter.gblur", sigma="5")
        second = self._effect("avfilter.gblur", sigma="9")
        changed = self._effect("avfilter.gblur", sigma="12")
        self.assertEqual(
            operation_name(self._clip_diff([first, second], [first, changed])),
            "effect.parameter_change",
        )

    def test_effect_work_on_tracks_and_master_is_attributed_too(self) -> None:
        before = self._effect("volume", level="0")
        after = self._effect("volume", level="-6")
        for entity, fallback_fields in (("track", {}), ("master_effect", {})):
            diff = {
                "changes": [
                    {
                        "entity": entity,
                        "native_id": 0,
                        "change": "updated",
                        "before": {"effects": [before], **fallback_fields},
                        "after": {"effects": [after], **fallback_fields},
                    }
                ]
            }
            self.assertEqual(operation_name(diff), "effect.parameter_change", entity)

    def test_track_state_change_alongside_effects_stays_a_state_change(self) -> None:
        # Claiming effect intent when the mute flag also moved would mislabel
        # the transaction, so the classifier only claims it when nothing else did.
        diff = {
            "changes": [
                {
                    "entity": "track",
                    "native_id": 0,
                    "change": "updated",
                    "before": {"muted": False, "effects": [self._effect("volume", level="0")]},
                    "after": {"muted": True, "effects": [self._effect("volume", level="-6")]},
                }
            ]
        }
        self.assertEqual(operation_name(diff), "track.set_state")

    def test_declared_effect_action_links_to_the_effect_diff(self) -> None:
        before = self._effect("qtblend", rect="0=0 0 100 100")
        after = self._effect("qtblend", rect="0=0 0 100 100;50=10 10 80 80")
        diff = event(2, "state.diff", boundary="commit", transaction_id="tx", diff=self._clip_diff([before], [after]))
        action = event(3, "action", action="effect.keyframe_change", transaction_id="tx", timeline_id="timeline", parameters={})

        reports = validate_action_semantics([diff, action], [diff])

        self.assertEqual(reports[0]["inferred"], "effect.keyframe_change")
        self.assertEqual(reports[0]["attribution"], "transaction")
        self.assertTrue(reports[0]["compatible"])
        self.assertEqual(reports[0]["status"], "passed")

    def test_generic_inference_does_not_degrade_a_specific_declared_effect_action(self) -> None:
        # An add plus a removal at different positions is real effect work the
        # classifier cannot narrow down; that limit must not read as a conflict.
        before = [self._effect("qtblend", rect="0 0 100 100")]
        after = [self._effect("avfilter.gblur", sigma="5"), self._effect("volume", level="-6")]
        self.assertEqual(operation_name(self._clip_diff(before, after)), "effect.change")
        diff = event(2, "state.diff", boundary="commit", transaction_id="tx", diff=self._clip_diff(before, after))
        action = event(3, "action", action="effect.add", transaction_id="tx", timeline_id="timeline", parameters={})

        reports = validate_action_semantics([diff, action], [diff])

        self.assertTrue(reports[0]["compatible"])
        self.assertEqual(reports[0]["status"], "passed")

    def test_keyframe_work_takes_precedence_over_a_static_tweak(self) -> None:
        # One transaction can carry both. Keyframe work is the more specific
        # claim and the harder signal to recover later, so it wins; pinning the
        # precedence keeps the label stable rather than order-dependent.
        before = self._effect("qtblend", rect="0=1 1 2 2", sigma="5")
        after = self._effect("qtblend", rect="0=1 1 2 2;9=3 3 4 4", sigma="7")
        self.assertEqual(
            operation_name(self._clip_diff([before], [after])),
            "effect.keyframe_change",
        )

    def test_keyframe_edit_on_an_existing_effect_is_not_read_as_an_add(self) -> None:
        # The case the record is mostly made of: the effect was already on the
        # clip and only its animation changed.
        before = self._effect("avfilter.dblur", **{"av.angle": "0=45"})
        after = self._effect("avfilter.dblur", **{"av.angle": "0=45;60=135"})
        self.assertEqual(
            operation_name(self._clip_diff([before], [after])),
            "effect.keyframe_change",
        )

    def test_coverage_metric_reports_the_unattributed_share(self) -> None:
        keyed = self._clip_diff(
            [self._effect("qtblend", rect="0=0 0 100 100")],
            [self._effect("qtblend", rect="0=0 0 100 100;50=10 10 80 80")],
        )
        moved = {
            "changes": [
                {
                    "entity": "clip",
                    "native_id": 1,
                    "change": "updated",
                    "before": {"timeline_start_frame": 0},
                    "after": {"timeline_start_frame": 10},
                }
            ]
        }
        linked = event(2, "state.diff", boundary="commit", transaction_id="tx-move", diff=moved)
        action = event(3, "action", action="clip.move", transaction_id="tx-move", timeline_id="timeline", parameters={})
        unlinked = event(4, "state.diff", boundary="commit", transaction_id="tx-effect", diff=keyed)
        command = event(5, "ui.command", interaction_id="i-1", command_id="c", label="l", source="menu", shortcuts=[])
        events = [linked, action, unlinked, command]

        coverage = attribution_coverage(validate_action_semantics(events, [linked, unlinked]), events)

        self.assertEqual(coverage["total_transactions"], 2)
        self.assertEqual(coverage["attributed_transactions"], 1)
        self.assertEqual(coverage["attributed_ratio"], 0.5)
        self.assertEqual(coverage["by_attribution"], {"state_only": 1, "transaction": 1})
        self.assertEqual(
            coverage["inferred_operations"],
            {"clip.move": 1, "effect.keyframe_change": 1},
        )
        self.assertEqual(coverage["ui_commands"], 1)
        self.assertEqual(coverage["distinct_interactions"], 1)


if __name__ == "__main__":
    unittest.main()
