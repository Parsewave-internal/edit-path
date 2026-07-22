<!--
SPDX-FileCopyrightText: 2026 Edit Path contributors
SPDX-License-Identifier: GPL-3.0-only
-->

# Video Path reconstruction operations

The production path is state-based. UI events remain audit evidence and useful
training context, but reconstruction loads the exact accepted Kdenlive/MLT
state and never attempts to replay mouse timing or shortcuts.

## Gate order

`edit-path process` runs these gates before publishing anything:

1. session envelope, contiguous sequence, lifecycle completion, and durable
   sidecar completion;
2. per-timeline semantic hash replay and the global exact-project hash chain;
3. deterministic commit/undo/redo branch resolution by transaction and
   undo-entry ID, including Qt merged undo commands;
4. action-to-diff semantic consistency and minimum accepted edit activity;
5. asset path, byte-count, and SHA-256 verification;
6. reconstruction and SSIM/duration/audio-structure validation at every
   checkpoint that has an independent capture-time proxy;
7. final exact-state render and comparison to the editor's final reference.

For 0.3 sessions, missing checkpoint references or exact state sidecars fail
closed. Legacy 0.1/0.2 samples remain readable, and a finalized legacy sample
may reconstruct from `internal/final.kdenlive`; unavailable legacy checkpoint
proxies are reported as skipped rather than retroactively invented.

An empty checkpoint can legitimately contain no video stream. In that one
case SSIM is reported as `not_applicable_no_video`; both files must be
video-free and must still pass duration and audio-structure checks.

## Local workflow

The CLI is platform-neutral. Use `python3` on Linux/macOS or `py -3` in a
source checkout on Windows. In the Windows portable package, change into
`bin` and use `.\python\python.exe -m edit_path` so the adjacent packaged
module is on Python's import path.

```bash
python3 -m edit_path doctor
python3 -m edit_path inspect /samples/session-001
python3 -m edit_path reconstruct /samples/session-001 --output /tmp/final.mp4
python3 -m edit_path process /samples/session-001 /dataset
python3 -m edit_path index /dataset
```

`doctor` must find Zstandard, Melt (or `mlt-melt`), FFmpeg, and FFprobe. A
Kdenlive project can also require MLT modules beyond the executables themselves.
Install the complete Kdenlive/MLT runtime when using effects, titles, Lottie,
or audio preview: the commonly required services include `avformat`, `xml`,
`qimage` or `pixbuf`, `kdenlivetitle`, Glaxnimate, Frei0r, AVFilter, and SDL or
RtAudio. The render gate fails instead of silently accepting a project when a
used service is unavailable.

### Container workflow on any host OS

Docker Engine on Linux and Docker Desktop on Windows/macOS avoid relying on the
host's media packages. Build the reviewed renderer once:

```bash
docker build -t edit-path-reconstruction -f reconstruction/Containerfile .
```

Then bind an absolute session directory read-only and a writable dataset
directory. The same command works in Bash; in PowerShell, replace the two
source values with absolute Windows paths and quote each complete `--mount`
argument.

```bash
docker run --rm \
  --mount type=bind,source=/absolute/path/session-001,target=/session,readonly \
  --mount type=bind,source=/absolute/path/dataset,target=/dataset \
  edit-path-reconstruction process /session /dataset
```

The container performs reconstruction only; the interactive EditPath/Kdenlive
collector still runs natively on the editor's operating system. The repository
verification script uses this container automatically when the host lacks
Melt or FFmpeg, so a missing host runtime cannot turn the real-media test into
a false success:

```bash
./scripts/run-verification.sh
```

The immutable accepted bundle contains `final.mp4`, a portable
`reconstructed.kdenlive`, cleaned and raw trajectories, exact state sidecars,
checkpoint references, hashed assets, the asset manifest, and a render report.
The entire directory is assembled under a temporary name and installed with
one atomic rename.

## Ingestion and worker pool

Ingestion performs all non-rendering gates, copies the complete source session,
revalidates that copy, and atomically places it on the filesystem queue:

```bash
python3 -m edit_path ingest /samples/session-001 /work-queue
python3 -m edit_path work-one /work-queue /dataset --runtime-lock /config/runtime-lock.json
```

`work-one` claims one job using an atomic directory rename, so multiple worker
containers can run the command concurrently. Raw inputs are retained under the
queue's `completed/` or `rejected/` archive after processing.

## Pinned renderer

`reconstruction/Containerfile` pins the base-image digest and renderer package
versions. Build it as a reviewed release artifact and run it with its final
image digest in `EDIT_PATH_CONTAINER_IMAGE`. Generate a runtime lock inside
that image, then require it on every production worker:

```bash
python3 -m edit_path lock-runtime /config/runtime-lock.json
python3 -m edit_path process /samples/session-001 /dataset \
  --runtime-lock /config/runtime-lock.json
```

The lock compares exact Melt/MLT and FFmpeg version strings and, when set, the
container image digest. Every render report also records the observed runtime.

## Collection-to-publication flow

```text
EditPath supervisor
  -> isolated Kdenlive + edit.kdenlive + session.json
  -> raw-events-NNN.jsonl + exact states + checkpoint references
  -> crash recovery and validated segment assembly when necessary
  -> content-addressed assets + independent editor render
  -> exact accepted-state reconstruction
  -> checkpoint and final media gates
  -> accepted/<session_id> or quarantine/<session_id>
```

The limited semantic cut/trim adapter remains diagnostic. Publication uses the
exact accepted Kdenlive/MLT state, so serializable effects, transitions, speed
changes, keyframes, titles, and other project state are retained when their MLT
services are installed. A missing service or media mismatch fails the render
gate and preserves evidence for inspection.

## Human QA and dataset assembly

```bash
python3 -m edit_path qa-sample /dataset --sample-rate 0.10
python3 -m edit_path qa-review /dataset SESSION_ID \
  --reviewer REVIEWER_ID --status passed --notes "Plausible edit path"
python3 -m edit_path index /dataset
```

Reviews live outside immutable bundles in `qa-reviews/`. A rejected review is
listed in `dataset-index.json.excluded` and is not exported as a training
sample. The license field remains additive and non-blocking for now; enable the
late publication gate with `--require-license` only after rights review is in
place.
