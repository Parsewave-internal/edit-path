<!-- SPDX-FileCopyrightText: 2026 Video Path Pilot contributors -->
<!-- SPDX-License-Identifier: GPL-3.0-only -->

# Editor workflow for the two-sample MVP

The normal editor workflow is entirely graphical. Double-click
`run-collector-app.sh` in the `video-path-pilot` folder and choose **Run** if
Linux asks whether to display or execute it. The first launch builds the small
collector window automatically; subsequent launches open directly.

Use a fresh directory for every sample. Do not reuse a crashed or incomplete
recording.

## Graphical workflow

1. Click **Create New Sample**.
2. Choose a new sample folder and enter editor ID, prompt, initial plan, project
   profile, and source asset files. Their displayed order becomes
   `asset_001`, `asset_002`, and so on.
3. Click **Create Sample**, then **Launch Instrumented Kdenlive**.
4. In Kdenlive, create a blank project with the displayed profile and import
   files from the sample's `assets/` directory in filename order.
5. Edit normally. In the collector window, use **Save Creative Decision** for
   meaningful choices—not every click.
6. Save the Kdenlive project, render the final video, and close Kdenlive
   normally.
7. In the collector, select the saved project and rendered video, write the
   final review, and click **Finalize and Validate**.
8. Watch the output completely and review `sample.json` before client delivery.

If Kdenlive crashes or is force-quit, create a fresh sample rather than reusing
the incomplete raw recording.

## Command-line fallback for developers

The commands below remain available for diagnosis and automated testing. Hired
editors do not need to use them.

### Initialize

```bash
python3 video-path-pilot/sample_collector.py init \
  /home/tenali/parsewave/samples/sample_001 \
  --editor-id editor_001 \
  --prompt "Create a 20-second energetic product montage." \
  --plan "Select the strongest moments, establish context, accelerate the cuts, and end on the product." \
  /path/to/video-a.mp4 /path/to/video-b.mp4 /path/to/music.wav
```

The command copies and hashes assets. It never modifies the originals.

### Launch and edit

```bash
python3 video-path-pilot/sample_collector.py launch \
  /home/tenali/parsewave/samples/sample_001
```

In Kdenlive, create a blank project with the requested resolution and frame
rate. Import files from the sample's `assets/` folder **in filename order**.
Edit normally. Save the project outside the sample or directly as
`internal/final.kdenlive`.

Add a rationale note when making a meaningful creative decision (not for every
click). Open another terminal and run:

```bash
python3 video-path-pilot/sample_collector.py note \
  /home/tenali/parsewave/samples/sample_001 \
  --reason "The opening felt slow." \
  --decision "Used three short detail shots before the wide shot to create momentum."
```

Render the final video, then close Kdenlive normally. A force-quit makes the
sample incomplete and it should be recollected.

### Finalize

```bash
python3 video-path-pilot/sample_collector.py finalize \
  /home/tenali/parsewave/samples/sample_001 \
  --project /path/to/saved-project.kdenlive \
  --output /path/to/rendered-video.mp4 \
  --review "The output follows the prompt; pacing and audio ending were checked."
```

Finalization validates the raw session, copies the native project and render,
hashes every artifact, removes undone work from the accepted branch, creates
`sample.json`, and validates the completed package.

### Human review before client delivery

Watch `output/final.*` completely. Open `sample.json` and verify that assets,
operation order, frames, prompt, plan, notes, and output are plausible. The MVP
marks every sample `needs_human_review`; a reviewer should record approval in
the client-delivery notes. Do not send local caches or unrelated project files.
