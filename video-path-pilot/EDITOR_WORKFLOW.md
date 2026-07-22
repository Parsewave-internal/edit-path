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

1. Click **Open Assigned Job** and select the provided `job.json`. The app shows
   the externally assigned task, project profile, and asset count.
2. Click **Start Editing Session**. The app creates a unique folder under the
   job's `sessions/` directory, launches an isolated Kdenlive configuration,
   and imports the job assets automatically.
3. Edit normally using the displayed task and supplied assets.
4. Save the native `.kdenlive` project and rendered final video inside the
   session folder displayed by the recorder.
5. Close Kdenlive normally. If it crashes, click **Recover and Continue**; the
   app preserves the prior segment and reopens the same isolated Kdenlive
   recovery context.
6. After recording validation, click **Finish Job**. The app resolves project
   bin IDs by asset SHA-256, normalizes the accepted edit path, generates and
   validates `sample.json`, and replays canonical state hashes.
7. Click **Open Completed Sample** and return the job directory.

The session folder contains at least:

```text
session_YYYYMMDD_HHMMSS/
├── raw-events-001.jsonl
├── kdenlive-console-001.log
├── final.kdenlive       # saved by editor
└── final.mp4            # rendered by editor
```

Crash recovery creates `raw-events-002.jsonl`, `raw-events-003.jsonl`, and so
on. Earlier incomplete segments remain auditable instead of being overwritten.

## Internal team workflow

The editor application generates `sample.json` automatically. The internal team
still performs final human review. Canonical replay is required. The initial
media adapter also reconstructs and renders ordinary cut/trim/move timelines;
effects, transitions, speed changes, and other unsupported features are clearly
reported and the sample is marked not ready for client review.
