# Create Dataset Sample / Edit Replay: execution report

This report follows the path after Kdenlive closes and the user clicks **Create Dataset Sample**. In the GUI this is also described as the edit-replay action.

## User sequence

Save and render one final video, close Kdenlive, wait for EditPath to validate the recording, then click **Create Dataset Sample**. The source workflow is [`EDITOR_WORKFLOW.md`](../video-path-pilot/EDITOR_WORKFLOW.md).

## GUI control flow

After Kdenlive exits, `editorFinished()` starts validation:

```cpp
const QString raw = QDir(m_session).filePath(
    QStringLiteral("EDIT-PATH/events-%1.jsonl").arg(m_segment, 3, 10, QLatin1Char('0')));
m_worker.start(pythonExecutable(), {
    m_repoRoot + QStringLiteral("/video-path-pilot/validate_video_path.py"), raw});
```

Only clean validation enables the button:

```cpp
if (success && !m_lastEditorExitCrashed && m_lastEditorExitCode == 0) {
    writeManifest(QStringLiteral("ready_to_finish"));
    m_finish->setEnabled(true);
}
```

The click handler launches finalization:

```cpp
void finishSession()
{
    m_finish->setEnabled(false);
    m_workerPurpose = QStringLiteral("finalize");
    m_worker.start(pythonExecutable(), {
        m_repoRoot + QStringLiteral("/video-path-pilot/job_pipeline.py"),
        QStringLiteral("finalize-freeform"), m_session});
}
```

## Finalization pipeline

`job_pipeline.py finalize-freeform` enters `process_session()` in `edit_path/pipeline.py`. It preflights the event envelope, state chain, assets, licenses, and edit minimums; validates checkpoint renders; locates the editor render; reconstructs the Kdenlive/MLT project; compares media; and renders the visual replay:

```python
preflight = preflight_session(session_dir, minimum_commits=minimum_commits,
    minimum_changed_entities=minimum_changed_entities,
    require_license=require_license, require_complete=require_complete)
process_video, process_video_report = render_edit_process(
    session_dir, events, branch.accepted, branch.baseline_hash,
    work_dir / "final.mp4", work_dir / "edit-process", melt_binary=melt_binary)
```

The result is atomically published as `completed-sample/`, containing the portable reconstructed project, copied assets, trajectory, editor final video, reconstructed output, replay video, and verification reports.

## Verification performed

Preflight passed: Python compilation, all JSON schemas, FFmpeg/FFprobe/Melt discovery, and CLI doctor. The full venv suite reported **61 passed, 2 skipped**; recovery tests passed ten consecutive repetitions.

I also ran the button-equivalent inspection against the available completed session:

```text
edit-path inspect /home/tripl/session_20260725_035405_14e6a95f
```

That old (2026-07-25) fixture stopped at sequence 12 with:

```text
state_error: branch before_hash does not equal current state 355656d8...
```

The event's `project_before_hash` is `ff24ade1...`, while the current checkpoint is `355656d8...`. This is an input-session inconsistency, not silent data loss: processing stops before publication and leaves the source untouched. Because the fixture predates the merged recorder changes, it is not evidence that a fresh current session fails. Weakening the state gate would hide a real broken chain.

## Conclusion

The click path is guarded and deterministic: validation must pass before the button is enabled; finalization uses a temporary work directory and atomic publication; failures preserve the original project/render. A true fresh GUI E2E test requires a new Kdenlive session, one edit, one render, close, and click.
