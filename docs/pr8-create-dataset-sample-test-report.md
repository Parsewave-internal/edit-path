# PR 8: Create Dataset Sample and replay stress-test report

Test window: 2026-08-29 (UTC). The checks below use the historical sessions that previously failed and a deterministic reasoning fixture. This report records observed output; it does not treat a generated label as evidence that was not present in the recording.

## Historical failure reproduction

| Session | Command/check | Observed result | Interpretation |
| --- | --- | --- | --- |
| `session_20260724_020218_248018a9/session_20260724_020218_248018a9` | `preflight_session(...)` | pass; 2,960 events, 340 accepted commits, 377 exact states | Valid source for replay testing |
| `session_20260725_035405_14e6a95f` | `preflight_session(...)` | `GateError: checkpoint exact state does not match the preceding project state` | Correctly rejected: the sidecar/hash chain cannot prove reconstruction |
| `session_20260725_035405_14e6a95f` | `job_pipeline.py finalize-freeform ...` | `ValueError: expected exactly one Kdenlive project ... found 4` | Correctly refuses ambiguous project selection; the fixture also fails the hash-chain gate above |
| `session_20260723_142444_4a8dea0b` | `preflight_session(...)` | `GateError: session is incomplete: final event is not session.end` | Correctly rejected as an unfinished/crashed recording |

The July 24 session's previous exact replay attempt rendered a full Kdenlive/Melt monitor for each state and then one FFmpeg process per event. It was still rendering after several minutes and produced no published replay when interrupted. That is the reproduced “hung” user experience.

## Bounded replay fix and proof

For trajectories over 500 moments, `render_edit_process` now uses `compact_semantic` mode: it preserves every event and operation in the report, skips the expensive exact monitor render for each state, renders 10 fps semantic UI scenes in parallel (up to four workers), and records the degradation explicitly in `state_preview_warnings`. Short trajectories retain the full exact-monitor path.

Regression test:

```text
python -m pytest -q tests/edit_path/test_reconstruction_pipeline.py::StateTests::test_large_replay_uses_bounded_compact_mode_without_exact_renders
1 passed
```

The test creates 501 moments, verifies that `render_event` is never called, verifies all 501 scenes are produced, and verifies the report says `render_mode=compact_semantic`, `render_fps=10`, and that exact previews were skipped.

The real July 24 run is written to `/tmp/edit-path-real-replay-proof2/` during this test window. The raw source has 2,960 events; 2,728 replay moments are visualized (all supported UI/state event types), with 377 exact states represented semantically. Its final `replay-result.json` contains the elapsed time, output SHA-256, FFprobe stream, event counts, and quality warnings. The run completed in 974.57 seconds (16.24 minutes), produced a 515,140,700-byte MP4, and reported `accepted=true`.

```text
codec_name=h264  width=1920  height=1080  r_frame_rate=10/1  avg_frame_rate=10/1  duration=1.000000
```

Final replay probe: `duration=1167.800000`, `nb_frames=11678`, `r_frame_rate=10/1`, SHA-256 `4d715753cdcb3641202f797c66e4b92d648dadeecb1bf7bd226b3a6987455c5f`. A frame captured at 600 seconds is [edit-replay-stress-preview.png](../live-example/edit-replay-stress-preview.png) (1920×1080, SHA-256 `a859c79c57a0db0e9988b6d40b3163ac476e66db51cbdb0126fea6ae7344e5d0`) and visibly contains the Project Bin, Project Monitor, Effects/Properties, multitrack timeline, and the active command card.

```text
{'accepted': True, 'moments': 2728, 'states': 377, 'accepted_edits': 340,
 'render_mode': 'compact_semantic', 'render_fps': 10,
 'duration_seconds': 1167.8, 'training_ui_quality': 'degraded',
 'state_preview_quality': 'degraded'}
```

The committed visual smoke sample is [edit-replay-demo-preview.png](../live-example/edit-replay-demo-preview.png), generated from [edit-replay-demo.mp4](../live-example/edit-replay-demo.mp4). Its report contains 7 moments, 2 exact states, and an accepted training UI replay.

## Think-aloud/audio proof

The recorder captures reasoning only after the editor clicks **Record Reasoning**. Audio is stored as FLAC under `EDIT-PATH/reasoning/audio-*.flac`; stop timestamps use monotonic nanoseconds and are aligned to overlapping/nearest event IDs. Transcription is opt-in and literal: the pipeline writes a transcript JSON and WebVTT captions and never infers an editor's intent.

The crash/resume path was also checked: both the Python capture helper and the Qt supervisor now choose the next unused `audio-NNN.flac` name, so restarting EditPath cannot overwrite an earlier think-aloud segment. The regression test pre-populates `audio-001.flac` and `audio-004.flac` and observes `audio-005.flac`.

Deterministic fixture output (FFmpeg-generated 2-second FLAC plus a fixed provider response) is under `/tmp/edit-path-reasoning-proof-20260829/`:

```text
audio-001.flac  sha256=81ec2a97a48e3ee244d3a9536547354bfdc3a18107e472c813d4ae6df56ca3e6
captions.vtt   sha256=b23f19d61e563ea08c0a1a48721ce16d31ecc37c1b9f938ec31eadf0efbe670c
aligned-proof.json sha256=9d221ecf6a72951255ea41a6f030023d043147df3b44096f1a14c414ba403bfd
```

The aligned record references the event occurring during the spoken interval (`cut-1`) plus `previous_event_id=before` and `next_event_id=after`. Whisper is not installed in this environment, so no fabricated Whisper result is reported; the GUI continues safely without transcription when the optional provider is unavailable.

## Client response model

1. **Per-action reasoning:** supported when the editor opts in. Capture and stop are tied to the same monotonic event clock as the raw journal; optional literal transcription produces time-aligned JSON/VTT. Existing historical sessions contain no reasoning audio, so their “why” cannot be recovered retroactively.
2. **Sidecars and manifest:** valid sessions publish verified state sidecars, the final project, and asset bindings by digest/original filename. Missing sidecars, an incomplete `session.end`, a broken hash chain, or ambiguous project files are rejected instead of guessed. The July 25 delivery is therefore not a reconstructable sample.
3. **Effects/keyframes:** raw recorder evidence remains broad (`state.diff`/`keyframe.update`). Python normalization derives labels such as `keyframe.value.change` from before/after state and reports unmapped commands/ambiguous attribution. PR 8 preserves those diagnostics and transaction/interaction IDs; it does not claim source-level detailed Kdenlive intent that the recorder did not emit.

## Verification matrix

```text
python -m pytest -q tests/edit_path video-path-pilot/tests
cmake -S video-path-pilot/gui -B /tmp/edit-path-gui-build -G Ninja && cmake --build /tmp/edit-path-gui-build -j2
```

The GUI build completed (`[2/2] Linking CXX executable EditPath`). No C++ recorder behavior was changed in this stress pass; changes are limited to bounded replay finalization, correct transcription-session diagnostics, and tests for both regressions.

Observed Python matrix result: `71 passed, 2 skipped in 14.74s`.
