<!-- SPDX-FileCopyrightText: 2026 Video Path Pilot contributors -->
<!-- SPDX-License-Identifier: GPL-3.0-only -->

# Editor workflow for the freeform recorder MVP

The editor may find, download, generate, or import media at any point. Nothing
must be prepared in the app before editing.

1. Double-click `run-collector-app.sh` and choose **Run**.
2. Click **Start Editing Session** and wait for the blank Kdenlive window.
   Remote X11 startup can take 15–60 seconds.
3. Make the requested video normally. Import or create assets whenever needed.
4. Save exactly one `.kdenlive` project directly in the session folder shown by
   the recorder.
5. Render exactly one final video (`.mp4`, `.mov`, `.mkv`, or `.webm`) into that
   same folder.
6. Close Kdenlive normally and wait for recording validation.
7. If Kdenlive ended unexpectedly, use **Recover and Continue**. A new numbered
   event segment is created without overwriting prior evidence.
8. Click **Finish Session**. The app discovers resources from the saved project,
   hashes and copies them, resolves Kdenlive IDs, generates `sample.json`,
   reconstructs supported edits, renders, and compares media.
9. Use **Open Generated Sample** to inspect the result.

The generated sample marks the verbal task prompt as
`pending_internal_entry`. The internal team attaches the exact instruction
before client review. No editor intent is collected.

Unsupported effects, transitions, speed changes, or other reconstruction gaps
do not destroy the sample; they are reported and keep
`ready_for_client_review` false.
