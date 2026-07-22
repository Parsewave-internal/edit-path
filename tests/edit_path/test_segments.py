# SPDX-FileCopyrightText: 2026 Edit Path contributors
# SPDX-License-Identifier: GPL-3.0-only

import json
import tempfile
import unittest
from pathlib import Path

from edit_path.errors import GateError
from edit_path.segments import assemble_segments
from edit_path.state import canonical_hash


CONTEXT = {
    "project_id": "project",
    "fps_numerator": 25,
    "fps_denominator": 1,
    "width": 320,
    "height": 180,
    "sample_aspect_numerator": 1,
    "sample_aspect_denominator": 1,
    "display_aspect_numerator": 16,
    "display_aspect_denominator": 9,
    "colorspace": 709,
    "progressive": True,
    "bottom_field_first": False,
    "audio_channels": 2,
    "audio_sample_rate": None,
    "kdenlive_version": "test",
    "kdenlive_build": "test",
    "mlt_version": "test",
}


def envelope(sequence: int, event_type: str, session_id: str, **values: object) -> dict:
    return {
        "schema_version": "0.3.0",
        "session_id": session_id,
        "sequence": sequence,
        "event_id": f"{session_id}-{sequence}",
        "timestamp_utc": "2026-07-22T00:00:00Z",
        "event_type": event_type,
        **values,
    }


def write_events(path: Path, events: list[dict]) -> None:
    path.write_text("".join(json.dumps(event) + "\n" for event in events), encoding="utf-8")


class SegmentAssemblyTests(unittest.TestCase):
    def make_segments(self, root: Path, *, break_continuity: bool = False) -> None:
        empty = {"timeline_id": "timeline", "duration_frames": 0, "tracks": [], "clips": [], "compositions": [], "mixes": [], "master_effects": []}
        inserted = {**empty, "duration_frames": 25, "clips": [{"native_id": 1, "entity_id": "clip", "asset_id": "asset", "track_native_id": 2}]}
        moved = {**inserted, "clips": [{**inserted["clips"][0], "timeline_start_frame": 10}]}
        first = [
            envelope(1, "session.start", "segment-a"),
            envelope(2, "project.context", "segment-a", context=CONTEXT),
            envelope(3, "state.checkpoint", "segment-a", timeline_id="timeline", label="baseline", snapshot=empty,
                     state_hash=canonical_hash(empty), project_state={"encoding": "zstd", "path": "states/a.zst", "sha256": "a" * 64, "bytes": 10},
                     reference_proxy={"path": "states/checkpoint_refs/a.mp4"}),
            envelope(4, "state.diff", "segment-a", timeline_id="timeline", label="insert", boundary="commit",
                     transaction_id="transaction-a", undo_entry_id="undo-a", before_hash=canonical_hash(empty), after_hash=canonical_hash(inserted),
                     project_before_hash="a" * 64, project_after_hash="b" * 64,
                     project_state={"encoding": "zstd", "path": "states/b.zst", "sha256": "b" * 64, "bytes": 10},
                     diff={"duration_after": 25, "changes": [{"entity": "clip", "native_id": 1, "change": "added", "after": inserted["clips"][0]}]}),
        ]
        recovered = empty if break_continuity else inserted
        recovery_change = (
            {"entity": "clip", "native_id": 1, "change": "added", "after": moved["clips"][0]}
            if break_continuity
            else {"entity": "clip", "native_id": 1, "change": "updated", "before": inserted["clips"][0], "after": moved["clips"][0]}
        )
        second = [
            envelope(1, "session.start", "segment-b"),
            envelope(2, "project.context", "segment-b", context=CONTEXT),
            envelope(3, "state.checkpoint", "segment-b", timeline_id="timeline", label="recovered", snapshot=recovered,
                     state_hash=canonical_hash(recovered), project_state={"encoding": "zstd", "path": "states/b.zst", "sha256": "b" * 64, "bytes": 10},
                     reference_proxy={"path": "states/checkpoint_refs/b.mp4"}),
            envelope(4, "state.diff", "segment-b", timeline_id="timeline", label="move", boundary="commit",
                     transaction_id="transaction-b", undo_entry_id="undo-b", before_hash=canonical_hash(recovered), after_hash=canonical_hash(moved),
                     project_before_hash="b" * 64, project_after_hash="c" * 64,
                     project_state={"encoding": "zstd", "path": "states/c.zst", "sha256": "c" * 64, "bytes": 10},
                     diff={"duration_after": 25, "changes": [recovery_change]}),
            envelope(5, "session.end", "segment-b", state_sidecars_complete=True),
        ]
        write_events(root / "raw-events-001.jsonl", first)
        write_events(root / "raw-events-002.jsonl", second)

    def test_recovery_segments_become_one_v03_trajectory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.make_segments(root)
            report = assemble_segments(root, session_id="collection")
            events = [json.loads(line) for line in Path(report["path"]).read_text(encoding="utf-8").splitlines()]
            self.assertEqual(report["segments"], 2)
            self.assertEqual([event["sequence"] for event in events], list(range(1, len(events) + 1)))
            self.assertEqual(sum(event["event_type"] == "session.start" for event in events), 1)
            self.assertEqual(sum(event["event_type"] == "project.context" for event in events), 1)
            self.assertEqual(sum(event["event_type"] == "session.recovered" for event in events), 1)
            self.assertTrue(all(event["session_id"] == "collection" for event in events))

    def test_recovery_rejects_discontinuous_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.make_segments(root, break_continuity=True)
            with self.assertRaisesRegex(GateError, "checkpoint"):
                assemble_segments(root)

    def test_pre_checkpoint_crash_segment_is_retained_as_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.make_segments(root)
            first = [
                envelope(1, "session.start", "early-crash"),
                envelope(2, "project.context", "early-crash", context=CONTEXT),
                envelope(3, "ui.command", "early-crash", interaction_id="interaction", command_id="play", label="Play",
                         source="programmatic_or_unknown", shortcuts=[]),
            ]
            write_events(root / "raw-events-000.jsonl", first)
            report = assemble_segments(root, session_id="collection")
            events = [json.loads(line) for line in Path(report["path"]).read_text(encoding="utf-8").splitlines()]
            self.assertEqual(report["segments"], 3)
            self.assertEqual(sum(event["event_type"] == "state.checkpoint" for event in events), 2)
            self.assertTrue(any(event.get("command_id") == "play" for event in events))


if __name__ == "__main__":
    unittest.main()
