# SPDX-License-Identifier: GPL-3.0-only

import json
import tempfile
import unittest
import zipfile
from pathlib import Path

import sys

sys.path.insert(0, str(Path(__file__).parents[1]))
from reliability import classify, create_snapshot, export_diagnostics, restore_recovery, select_recovery


class ReliabilityTests(unittest.TestCase):
    def project(self, root: Path, content: str = "<mlt></mlt>") -> Path:
        path = root / "edit.kdenlive"
        path.write_text(content, encoding="utf-8")
        return path

    def test_snapshot_is_atomic_versioned_and_deduplicated(self):
        with tempfile.TemporaryDirectory() as temporary:
            session = Path(temporary)
            project = self.project(session)
            first = create_snapshot(session)
            duplicate = create_snapshot(session)
            project.write_text("<mlt><producer/></mlt>", encoding="utf-8")
            second = create_snapshot(session)
            self.assertTrue(first["created"])
            self.assertFalse(duplicate["created"])
            self.assertTrue(second["created"])
            self.assertEqual(len(list((session / "recovery").glob("project-*.kdenlive"))), 2)

    def test_invalid_project_never_replaces_good_snapshot(self):
        with tempfile.TemporaryDirectory() as temporary:
            session = Path(temporary)
            project = self.project(session)
            create_snapshot(session)
            project.write_text("<mlt>", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "rejected"):
                create_snapshot(session)
            selected = select_recovery(session)["selected"]
            self.assertIn("recovery", selected["path"])
            restored = restore_recovery(session)
            self.assertTrue(restored["restored"])
            self.assertEqual(project.read_text(), "<mlt></mlt>")

    def test_retention_preserves_milestones(self):
        with tempfile.TemporaryDirectory() as temporary:
            session = Path(temporary)
            project = self.project(session)
            create_snapshot(session, "pre_render", keep=2)
            for index in range(4):
                project.write_text(f"<mlt><property>{index}</property></mlt>", encoding="utf-8")
                create_snapshot(session, "periodic", keep=2)
            manifest = json.loads((session / "recovery/manifest.json").read_text())
            self.assertEqual(sum(item["reason"] == "periodic" for item in manifest["snapshots"]), 2)
            self.assertTrue(any(item["reason"] == "pre_render" for item in manifest["snapshots"]))

    def test_missing_session_end_is_not_classified_as_native_crash(self):
        with tempfile.TemporaryDirectory() as temporary:
            session = Path(temporary)
            self.project(session)
            (session / "session.json").write_text(
                json.dumps({"status": "validation_failed", "last_exit_code": 0, "last_exit_crashed": False})
            )
            (session / "raw-events-001.jsonl").write_text(
                json.dumps({"event_type": "session.start"}) + "\n", encoding="utf-8"
            )
            self.assertEqual(classify(session)["failure_type"], "missing_session_end")

    def test_decoder_errors_take_precedence_over_missing_end(self):
        with tempfile.TemporaryDirectory() as temporary:
            session = Path(temporary)
            self.project(session)
            (session / "session.json").write_text(json.dumps({"status": "validation_failed"}))
            (session / "kdenlive-console-001.log").write_text("Invalid NAL unit size\nmissing picture in access unit")
            diagnosis = classify(session)
            self.assertEqual(diagnosis["failure_type"], "media_decode_error")
            self.assertEqual(diagnosis["decoder_errors"]["invalid_nal_unit_size"], 1)

    def test_diagnostics_zip_sanitizes_home_paths(self):
        with tempfile.TemporaryDirectory() as temporary:
            session = Path(temporary) / "session-test"
            session.mkdir()
            self.project(session)
            (session / "session.json").write_text(json.dumps({"status": "start_failed"}))
            (session / "kdenlive-console-001.log").write_text(r"C:\Users\Alice\Videos\secret.mp4")
            output = export_diagnostics(session)
            with zipfile.ZipFile(output) as archive:
                text = archive.read("session/kdenlive-console-001.log").decode()
                self.assertNotIn("Alice", text)
                self.assertIn("<USER_HOME>", text)
                diagnosis_text = archive.read("diagnosis.json").decode()
                self.assertNotIn(str(Path.home()), diagnosis_text)
                self.assertEqual(json.loads(diagnosis_text)["failure_type"], "startup_failure")


if __name__ == "__main__":
    unittest.main()
