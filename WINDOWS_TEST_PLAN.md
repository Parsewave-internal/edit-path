# Windows MVP acceptance test

Use the synthetic files shipped in the portable package's `test-media` folder.
Perform the normal-session test before the crash-recovery test.

## Editing assignment

> Create a 12–18 second 1280×720 video using both supplied video assets. Cut
> unwanted sections, arrange material from both videos, add the supplied audio
> track, adjust its timing, perform at least one undo and redo, and render the
> final result as MP4. This is an operational test; no editor explanation or
> creative intent should be entered.

## Test A: normal session

1. Extract `EditPath-Windows-x64.zip` to a normal writable folder.
2. Double-click `bin\EditPath.exe`. Do not open `kdenlive.exe`.
3. Confirm Kdenlive opens directly with no terminal or initialization screen.
4. Import all three files from `test-media`.
5. Add both videos to the timeline and make at least two cuts.
6. Move or trim one clip at a visible frame boundary.
7. Add `test-audio.wav` and move it to a deliberate timeline position.
8. Press **Ctrl+Z** once and **Ctrl+Shift+Z** once.
9. Save normally. Confirm no second project filename is requested and the
   session contains `edit.kdenlive`.
10. Render one MP4 directly into the displayed session folder.
11. Close Kdenlive normally.
12. Confirm the Edit Path completion screen appears, then click **Finish
    Session**.
13. Open the generated sample and confirm `sample.json`, assets, final video,
    raw events, native project, and validation reports exist.

Record PASS/FAIL and notes for every check:

- Kdenlive opened directly.
- No terminal/init screen appeared.
- Editing and preview worked.
- `edit.kdenlive` was created.
- Final MP4 rendered.
- Completion screen appeared only after closing Kdenlive.
- `sample.json` was generated.
- Operations contain integer frame positions/state changes.
- `history.undo` and `history.redo` are present.
- Asset IDs and SHA-256 hashes are present.
- Reconstruction report exists and states passed, unsupported, or failed with
  an explicit reason.

## Test B: crash recovery

1. Start `bin\EditPath.exe` again.
2. Import `test-video-1.mp4`, put it on the timeline, and press **Ctrl+S**.
3. Make another visible edit and press **Ctrl+S** again.
4. Open Windows Task Manager, select Kdenlive, and choose **End task**. Do not
   terminate EditPath.
5. Confirm the recovery screen appears.
6. Choose **Recover and Continue**.
7. Confirm `edit.kdenlive` reopens and the saved timeline edit remains.
8. Make one additional edit, save, render an MP4, and close normally.
9. Finish the session and confirm multiple numbered raw-event and console-log
   segments were retained.

Do not report a test as passed if Kdenlive merely opened. A successful MVP test
must complete packaging and inspect the resulting `sample.json`.
