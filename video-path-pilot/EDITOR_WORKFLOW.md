<!-- SPDX-FileCopyrightText: 2026 Video Path Pilot contributors -->
<!-- SPDX-License-Identifier: GPL-3.0-only -->

# Editor workflow for the recorder MVP

The editor-facing application records editing behavior and outcomes only. It
does not collect the editor's plan, reasoning, decisions, rationale, or review.
The Parsewave team constructs the two client samples after the sessions.

## Start the app

Double-click `run-collector-app.sh` and choose **Run** if Linux asks whether to
display or execute it. No terminal commands are part of the editor workflow.

## Record a session

1. Click **Start Editing Session**. The app automatically creates a unique
   folder under `Videos/EditPathSessions/` and launches an isolated Kdenlive
   configuration that does not reopen the previous project.
2. Edit normally using the task instructions and assets supplied separately.
3. Save the native `.kdenlive` project and rendered final video inside the
   session folder displayed by the recorder.
4. Close Kdenlive normally. Do not force-quit it.
5. Wait until the recorder reports that `raw-events.jsonl` passed validation.
6. Click **Open Session Folder** and return the complete folder plus the source
   assets to the Parsewave team.

The session folder contains at least:

```text
session_YYYYMMDD_HHMMSS_xxxxxxxx/
├── raw-events.jsonl
├── kdenlive-console.log
├── final.kdenlive       # saved by editor
└── final.mp4            # rendered by editor
```

If Kdenlive crashes or the recorder reports an incomplete session, create a
fresh session and repeat the edit.

## Internal team workflow

The editor does not generate `sample.json`. After receiving the session,
project, render, assets, and externally assigned task prompt, the internal team
validates the evidence, resolves project asset identities, normalizes the
accepted edit path, packages the sample, and performs human quality review.
