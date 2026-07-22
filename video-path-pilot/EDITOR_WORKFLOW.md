<!-- SPDX-FileCopyrightText: 2026 Video Path Pilot contributors -->
<!-- SPDX-License-Identifier: GPL-3.0-only -->

# Editor workflow for the freeform recorder MVP

The editor may find, download, generate, or import media at any point. Nothing
must be prepared in the app before editing.

1. Start `bin\EditPath.exe` from the Windows portable package, or run
   `./video-path-pilot/run-collector-app.sh` on Linux/macOS. The supervisor
   stays hidden and Kdenlive opens directly; remote X11 startup can take
   15–60 seconds.
2. A fresh installation starts the edit without an initialization form. On a
   later launch, EditPath may first show the previous completed or interrupted
   session; choose **Start New Edit** or **Resume Editing** as appropriate.
3. Make the requested video normally. Import or create assets whenever needed.
4. The recorder creates `edit.kdenlive` in the session folder automatically.
   Save normally while editing; do not create a second project file.
5. Render exactly one final video (`.mp4`, `.mov`, `.mkv`, or `.webm`) into that
   same folder.
6. Close Kdenlive normally. The completion screen then appears and validates
   the recording.
7. If Kdenlive ended unexpectedly, restart the recorder and use **Resume
   Editing**. It reopens `edit.kdenlive` (and Kdenlive may offer its latest
   autosave) while creating a new numbered event segment without overwriting
   prior evidence.
8. Click **Create Dataset Sample**. The app discovers resources from the saved project,
   hashes and copies them, resolves Kdenlive IDs, generates `sample.json`,
   reconstructs the exact accepted Kdenlive/MLT state, renders, and compares
   media.
9. Use **Open Dataset Sample** to inspect the result. `sample.json` is the
   authoritative record; see `../DATASET_ITEM.md` for the packaged layout.

The generated sample marks the verbal task prompt as
`pending_internal_entry`. The internal team attaches the exact instruction
before client review. No editor intent is collected.

Effects, transitions, speed changes, keyframes, titles, and other serializable
project state are retained by exact-state reconstruction when the corresponding
MLT service is installed. A missing service or media mismatch does not destroy
the evidence: finalization fails with an explicit gate/reason and leaves the
session available for correction or quarantine review.
