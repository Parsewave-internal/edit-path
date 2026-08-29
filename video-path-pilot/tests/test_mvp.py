# SPDX-FileCopyrightText: 2026 Video Path Pilot contributors
# SPDX-License-Identifier: GPL-3.0-only

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1]))
from job_pipeline import (canonical_hash, discover_rendered_video, embedded_project_assets,
                          organize_dataset_item, project_render_output, project_resources, replay_report,
                          resolve_assets, used_asset_references)
from normalize_sample import accepted_commits, build_sample
from sample_collector import bind_project_assets
from validate_sample import validate_sample
from validate_video_path import validate as validate_raw

HASH_B = "b" * 64


class MvpTests(unittest.TestCase):
    def test_completed_sample_has_role_oriented_layout(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for relative, content in {
                "assets/a.mp4": b"asset",
                "reference/editor-final.mp4": b"target",
                "final.mp4": b"replay",
                "trajectory.jsonl": b"",
                "reconstructed-output.mp4": b"reconstructed",
                "reconstructed.kdenlive": b"<property>assets/a.mp4</property>",
                "render-report.json": b"{}",
                "internal/final.kdenlive": b"native",
                "asset-manifest.json": json.dumps({"assets": [{"asset_id": "asset_001", "file": "assets/a.mp4", "sha256": "x", "bytes": 5, "bin_references": ["4"]}]}).encode(),
            }.items():
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(content)
            organize_dataset_item(root, ".mp4")
            self.assertTrue((root / "outputs/final.mp4").is_file())
            self.assertTrue((root / "edit-path/replay.mp4").is_file())
            self.assertTrue((root / "verification/reconstructed.mp4").is_file())
            self.assertTrue((root / "provenance/editor-project.kdenlive").is_file())
            self.assertIn(b"../inputs/assets/a.mp4", (root / "verification/reconstructed.kdenlive").read_bytes())
            bindings = json.loads((root / "provenance/asset-bindings.json").read_text())
            self.assertEqual(bindings["bindings"], [{"asset_id": "asset_001", "file": "assets/a.mp4",
                                                       "sha256": "x", "bytes": 5, "bin_references": ["4"]}])

    def test_recorder_binds_delayed_actions_before_buffering(self):
        source = (Path(__file__).parents[2] / "src/videopath/videopathrecorder.cpp").read_text(encoding="utf-8")
        record_action = source.split("void VideoPathRecorder::recordAction", 1)[1].split(
            "void VideoPathRecorder::flushPendingActions", 1
        )[0]
        self.assertLess(
            record_action.index("addTransactionFields(event, true)"),
            record_action.index("m_pendingActions.append(event)"),
        )

    def test_recorder_effect_intent_uses_canonical_taxonomy(self):
        source = (Path(__file__).parents[2] / "src/videopath/videopathrecorder.cpp").read_text(encoding="utf-8")
        for label in (
            "effect.add", "effect.remove", "effect.reorder", "effect.parameter.change",
            "keyframe.add", "keyframe.remove", "keyframe.multi_edit", "keyframe.value.change",
        ):
            self.assertIn(f'QStringLiteral("{label}")', source)
        for stale in ("effect.change", "keyframe.update"):
            self.assertNotIn(f'QStringLiteral("{stale}")', source)

    def test_recorder_effect_acceptance_has_stable_ids_and_property_scope(self):
        source = (Path(__file__).parents[2] / "src/videopath/videopathrecorder.cpp").read_text(encoding="utf-8")
        for marker in (
            'QStringLiteral("effect_id")', 'QStringLiteral("parameter_id")',
            'QStringLiteral("keyframe_id")', 'QStringLiteral("property_editor")',
            'QStringLiteral("synthetic_state_correlation")',
        ):
            self.assertIn(marker, source)

    def test_supervisor_reasoning_capture_has_mic_test_playback_and_python_bridge(self):
        root = Path(__file__).parents[2]
        source = (root / "video-path-pilot/gui/main.cpp").read_text(encoding="utf-8")
        for marker in (
            "Configure microphone",
            "List available microphones",
            "Test microphone and play it back",
            "captureMicrophoneTest",
            "QMediaDevices::audioInputs()",
            "new QComboBox",
            "device->currentText()",
            "EditPathAudio.exe",
            "native WASAPI",
            "m_micPlayer->play()",
            "m_audioCapture.write(\"q\\n\")",
            "GenericDataLocation",
            "whisperPythonExecutable",
            "startWorker(whisperPythonExecutable()",
            "showWorkProgress",
            "Working: Whisper transcription is in progress",
            "QProcess::errorOccurred",
            "QApplication::processEvents(QEventLoop::ExcludeUserInputEvents)",
        ):
            self.assertIn(marker, source)
        installer = (root / "packaging/windows/dependency-installer.ps1").read_text(encoding="utf-8")
        self.assertIn("installed into EditPath venv", installer)
        self.assertNotIn("pip install --disable-pip-version-check -U openai-whisper --user", installer)
        launcher = (root / "packaging/windows/install-dependencies.bat").read_text(encoding="utf-8")
        self.assertIn("dependency-installer.exe %*", launcher)
        self.assertIn("ExecutionPolicy bypass", launcher)
        build = (root / "packaging/windows/build-editpath.ps1").read_text(encoding="utf-8")
        self.assertIn('install-dependencies.bat', build)
        whisper = (root / "edit_path/whisper_provider.py").read_text(encoding="utf-8")
        self.assertIn('sys.executable, "-m", "whisper"', whisper)

    def test_native_audio_helper_is_built_and_packaged(self):
        root = Path(__file__).parents[2]
        helper = (root / "video-path-pilot/gui/audio-helper.cpp").read_text(encoding="utf-8")
        cmake = (root / "video-path-pilot/gui/CMakeLists.txt").read_text(encoding="utf-8")
        build = (root / "packaging/windows/build-editpath.ps1").read_text(encoding="utf-8")
        for marker in ("QMediaDevices::audioInputs", "QAudioSource", "WavWriter", "recording_started", "recording_stopped"):
            self.assertIn(marker, helper)
        self.assertIn("OUTPUT_NAME EditPathAudio", cmake)
        self.assertIn("EditPathAudio.exe is missing", build)
        self.assertIn("@($editPath, $audioHelper, $kdenlive)", build)

    def test_supervisor_hardening_contract(self):
        root = Path(__file__).parents[2]
        source = (root / "video-path-pilot/gui/main.cpp").read_text(encoding="utf-8")
        launcher = (root / "video-path-pilot/run-collector-app.sh").read_text(encoding="utf-8")
        self.assertIn('writeManifest(QStringLiteral("validation_failed"))', source)
        self.assertIn("m_lastEditorExitCrashed = exitStatus != QProcess::NormalExit || exitCode != 0", source)
        self.assertIn("QApplication::clipboard()->setText(nativePath)", source)
        self.assertIn('environment.remove(QStringLiteral("QSG_RHI_BACKEND"))', source)
        self.assertIn('config.setValue(QStringLiteral("parallelrender"), false)', source)
        self.assertIn('config.setValue(QStringLiteral("processingthreads"), 1)', source)
        self.assertIn('config.setValue(QStringLiteral("encodethreads"), 2)', source)
        self.assertIn('environment.insert(QStringLiteral("TEMP")', source)
        self.assertIn("unset QSG_RHI_BACKEND LIBGL_ALWAYS_SOFTWARE", launcher)

    def test_supervisor_uses_editor_facing_language(self):
        root = Path(__file__).parents[2]
        source = (root / "video-path-pilot/gui/main.cpp").read_text(encoding="utf-8")
        launcher = (root / "video-path-pilot/run-collector-app.sh").read_text(encoding="utf-8")
        self.assertIn('QStringLiteral("Create Dataset Sample")', source)
        self.assertIn('QStringLiteral("Resume Editing")', source)
        self.assertIn('QStringLiteral("Show technical details")', source)
        self.assertIn("m_activity->setVisible(false)", source)
        self.assertIn("friendlyFinalizationError(m_workerTranscript)", source)
        self.assertIn('value.contains(QStringLiteral("found 0"))', source)
        self.assertIn("We couldn't find your final rendered video", source)
        self.assertIn('completed-sample/verification/report.json', source)
        self.assertNotIn('QStringLiteral("Sample generation failed: %1")', source)
        self.assertIn('m_start->setText(QStringLiteral("Start New Edit Anyway"))', source)
        self.assertIn('QMessageBox::question(this, QStringLiteral("Start a new edit?")', source)
        self.assertNotIn("else if (m_autoRecover)", source)
        self.assertIn("Kdenlive closed unexpectedly, but your saved work is available", source)
        self.assertIn('KDENLIVE_VIDEO_PATH_READY_FILE', source)
        self.assertIn('KDENLIVE_VIDEO_PATH_RENDER_OUTPUT', source)
        self.assertIn("No file copying or dragging is required.", source)
        self.assertIn('m_launchProgress->setRange(0, 0)', source)
        self.assertIn('m_readyFile + QStringLiteral(".ack")', source)
        self.assertIn('QStringLiteral("supervisor-activity.log")', source)
        self.assertIn('writeManifest(QStringLiteral("recovery_available"))', source)
        self.assertIn('setsid --wait "$binary"', launcher)
        self.assertIn("restart_count > 3", launcher)

        editor_main = (root / "src/main.cpp").read_text(encoding="utf-8")
        self.assertIn('qEnvironmentVariableIntValue("KDENLIVE_VIDEO_PATH_AUTOSAVE_MS"', editor_main)
        self.assertIn("document->isModified()", editor_main)
        self.assertIn("pCore->projectManager()->saveFile()", editor_main)
        self.assertIn('qEnvironmentVariable("KDENLIVE_VIDEO_PATH_READY_FILE")', editor_main)
        self.assertIn('qEnvironmentVariable("KDENLIVE_VIDEO_PATH_RENDER_OUTPUT")', editor_main)
        self.assertIn('setDocumentProperty(QStringLiteral("renderurl"), recorderRenderOutput)', editor_main)
        self.assertIn('readyFile.write("ready\\n")', editor_main)
        self.assertIn('document == nullptr || document->loading', editor_main)
        self.assertIn('captureTimelineCheckpoint(QStringLiteral("gui.ready"))', editor_main)
        self.assertNotIn('QTimer::singleShot(1000, &app, [recorderReadyFile]', editor_main)

    def test_timewarp_producer_resolves_to_its_source_asset(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            media = root / "source.mp4"
            media.write_bytes(b"media")
            project = root / "edit.kdenlive"
            project.write_text(
                f'''<mlt root="{root}"><profile frame_rate_num="25" frame_rate_den="1" width="1920" height="1080"/>
                <chain><property name="kdenlive:id">5</property><property name="mlt_service">avformat</property><property name="resource">{media}</property></chain>
                <producer><property name="kdenlive:id">5</property><property name="mlt_service">timewarp</property><property name="resource">2.25:{media}</property></producer></mlt>''',
                encoding="utf-8",
            )
            resources, _ = project_resources(project)
            self.assertEqual(resources, {"5": media})

    def test_windows_build_uses_supported_visual_studio_and_craft_launcher(self):
        root = Path(__file__).parents[2]
        source = (root / "packaging/windows/build-editpath.ps1").read_text(encoding="utf-8")
        self.assertIn('-version "[17.0,18.0)"', source)
        self.assertIn('$env:CRAFT_PYTHON', source)
        self.assertIn('"bin\\craft.py"', source)
        self.assertIn(
            '& $craftPython $craftScript --ci-mode --no-cache --ignoreInstalled --options "kde/kdemultimedia/kdenlive.srcDir=$sourceRoot"',
            source,
        )
        self.assertIn('@("sh.exe", "gcc.exe", "g++.exe", "cpp.exe")', source)

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

    def test_legacy_final_branch_helper(self):
        a = {"event_type": "state.diff", "boundary": "commit", "event_id": "a"}
        b = {"event_type": "state.diff", "boundary": "commit", "event_id": "b"}
        undo = {"event_type": "state.diff", "boundary": "undo"}
        redo = {"event_type": "state.diff", "boundary": "redo"}
        self.assertEqual(accepted_commits([a, b, undo]), [a])
        self.assertEqual(accepted_commits([a, b, undo, redo]), [a, b])

    def test_project_resources_accept_file_uri(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            media = root / "media clip.mp4"
            media.write_bytes(b"video")
            project = root / "project.kdenlive"
            project.write_text(f'''<mlt root=".">
              <profile frame_rate_num="25" frame_rate_den="1" width="1920" height="1080"/>
              <producer><property name="kdenlive:id">7</property>
              <property name="resource">{media.as_uri()}</property></producer>
            </mlt>''', encoding="utf-8")
            resources, _ = project_resources(project)
            self.assertEqual(resources, {"7": media.resolve()})

    def test_embedded_kdenlive_title_is_resolved_without_a_fake_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            media = root / "image.png"
            media.write_bytes(b"image")
            project = root / "edit.kdenlive"
            project.write_text(f'''<mlt root="{root}">
              <profile frame_rate_num="25" frame_rate_den="1" width="1920" height="1080"/>
              <producer><property name="kdenlive:id">4</property><property name="mlt_service">qimage</property>
                <property name="resource">{media}</property></producer>
              <producer><property name="kdenlive:id">5</property><property name="mlt_service">kdenlivetitle</property>
                <property name="resource"></property><property name="xmldata">&lt;kdenlivetitle/&gt;</property></producer>
              <producer><property name="kdenlive:id">7</property><property name="mlt_service">avformat</property>
                <property name="resource">{root / 'missing.mp4'}</property></producer>
              <tractor><property name="kdenlive:id">6</property><property name="kdenlive:producer_type">17</property></tractor>
            </mlt>''', encoding="utf-8")
            resources, _ = project_resources(project)
            embedded = embedded_project_assets(project)
            self.assertEqual(resources, {"4": media.resolve(), "7": (root / "missing.mp4").resolve()})
            self.assertEqual(set(embedded), {"5", "6"})
            self.assertEqual(embedded["5"]["service"], "kdenlivetitle")

            raw = root / "raw-events.jsonl"
            raw.write_text(json.dumps({"event_type": "state.diff", "diff": {"changes": [
                {"entity": "clip", "change": "added", "after": {"asset_reference": "5"}}
            ]}}) + "\n", encoding="utf-8")
            self.assertEqual(used_asset_references([raw]), {"5"})
            self.assertFalse(used_asset_references([raw]) - set(embedded))

    def test_embedded_title_normalizes_as_a_resolved_project_asset(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "evidence").mkdir()
            raw = root / "evidence" / "raw-events.jsonl"
            raw.write_text(json.dumps({
                "event_type": "state.diff", "boundary": "commit", "event_id": "title-add", "sequence": 1,
                "after_hash": HASH_B, "diff": {"changes": [{"entity": "clip", "native_id": 9,
                    "change": "added", "after": {"asset_reference": "5", "asset_id": "stable-title-id",
                    "duration_frames": 25}}]},
            }) + "\n", encoding="utf-8")
            sample = build_sample(root, {
                "sample_id": "title-sample", "prompt": "Add a title",
                "project": {"frame_rate": {"numerator": 25, "denominator": 1}, "width": 1920, "height": 1080},
                "assets": [], "asset_binding_method": "project_resource_sha256",
                "embedded_project_assets": {"5": {"native_reference": "5", "kind": "generator",
                    "service": "kdenlivetitle", "project_content_sha256": "a" * 64}},
                "artifacts": {"final_video": "output/final.mp4", "final_video_sha256": "b" * 64,
                    "native_project": "internal/final.kdenlive", "native_project_sha256": "c" * 64,
                    "raw_events": [{"file": "evidence/raw-events.jsonl"}]},
            })
            self.assertEqual(sample["quality"]["unresolved_asset_ids"], [])
            self.assertEqual(sample["inputs"]["embedded_assets"][0]["asset_id"], "stable-title-id")
            self.assertEqual(sample["edit_path"]["operations"][0]["changes"][0]["after"]["asset_id"], "stable-title-id")
            sample_path = root / "sample.json"
            sample_path.write_text(json.dumps(sample), encoding="utf-8")
            self.assertEqual(validate_sample(sample_path), [])

    def test_render_output_is_discovered_from_saved_kdenlive_destination(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            session = root / "session"
            session.mkdir()
            external = root / "Videos" / "client-final.mkv"
            external.parent.mkdir()
            external.write_bytes(b"render")
            project = session / "edit.kdenlive"
            project.write_text(
                f'''<mlt root="{session}"><playlist id="main_bin">
                <property name="kdenlive:docproperties.renderurl">{external}</property>
                </playlist></mlt>''',
                encoding="utf-8",
            )
            self.assertEqual(project_render_output(project), external.resolve())
            self.assertEqual(discover_rendered_video(session, project), external.resolve())

            managed = session / "editor-final.mp4"
            managed.write_bytes(b"managed render")
            self.assertEqual(discover_rendered_video(session, project), managed.resolve())

    def test_missing_saved_render_has_actionable_error(self):
        with tempfile.TemporaryDirectory() as temporary:
            session = Path(temporary)
            missing = session.parent / "missing-final.mp4"
            project = session / "edit.kdenlive"
            project.write_text(
                f'''<mlt><playlist><property name="kdenlive:docproperties.renderurl">{missing}</property></playlist></mlt>''',
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "did not complete or no longer exists"):
                discover_rendered_video(session, project)

    def test_legacy_sample_without_checkpoint_stays_supported(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for directory in ("assets", "output", "internal", "evidence"):
                (root / directory).mkdir()
            files = {
                "assets/asset.mp4": b"asset",
                "output/final.mp4": b"video",
                "internal/final.kdenlive": b"project",
            }
            for name, contents in files.items():
                (root / name).write_bytes(contents)
            events = [{"event_type": "state.diff", "boundary": "commit", "event_id": "event", "sequence": 1,
                       "after_hash": HASH_B, "diff": {"changes": [{"entity": "clip", "native_id": 1,
                       "change": "added", "after": {"asset_reference": "4", "duration_frames": 25}}]}}]
            raw = root / "evidence/raw-events.jsonl"
            raw.write_text("".join(json.dumps(event) + "\n" for event in events), encoding="utf-8")
            digest = lambda path: hashlib.sha256(path.read_bytes()).hexdigest()
            sample = build_sample(root, {
                "sample_id": "legacy", "prompt": "Legacy edit",
                "project": {"frame_rate": {"numerator": 25, "denominator": 1}, "width": 1920, "height": 1080},
                "assets": [{"asset_id": "asset_001", "file": "assets/asset.mp4", "sha256": digest(root / "assets/asset.mp4"), "bytes": 5, "bin_reference": "4"}],
                "asset_binding_method": "project_resource_sha256",
                "artifacts": {"final_video": "output/final.mp4", "final_video_sha256": digest(root / "output/final.mp4"),
                              "native_project": "internal/final.kdenlive", "native_project_sha256": digest(root / "internal/final.kdenlive"),
                              "raw_events": "evidence/raw-events.jsonl"},
            })
            self.assertNotIn("initial_state", sample["edit_path"])
            self.assertEqual(sample["edit_path"]["operations"][0]["operation"], "clip.insert")

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
                 "before_hash": canonical_hash(baseline), "after_hash": canonical_hash(after), "diff": {"duration_after": 25, "changes": [
                    {"entity": "clip", "native_id": 8, "change": "added", "after": after["clips"][0]}]}},
            ]
            path.write_text("".join(json.dumps(event) + "\n" for event in events))
            report = replay_report([path])
            self.assertEqual(report["canonical_state_replay"], "passed")
            self.assertEqual(report["reconstructed_render"], "not_implemented")


if __name__ == "__main__":
    unittest.main()
