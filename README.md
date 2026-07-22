![](data/pics/kdenlive-logo.png)

# Kdenlive

Kdenlive is a powerful, free and open-source video editor that brings professional-grade video editing capabilities to everyone. Whether you're creating a simple family video or working on a complex project, Kdenlive provides the tools you need to bring your vision to life.

## Edit Path trajectory capture and reconstruction

This fork adds an opt-in recording and reconstruction system for producing
auditable video-editing trajectories and deterministic training samples. It
records semantic timeline changes and exact Kdenlive/MLT project states; it
does not attempt to reproduce a session by replaying mouse gestures or keyboard
timing.

The production pipeline is:

```text
JSONL trajectory + content-addressed assets + exact state sidecars
  -> validate session, semantic hashes, and global project hash chain
  -> resolve the accepted commit/undo/redo branch
  -> reconstruct and validate every captured checkpoint
  -> render the final accepted state with pinned Melt/MLT and FFmpeg
  -> gate on FFprobe structure, duration, audio structure, and video SSIM
  -> atomically publish an accepted sample or a detailed quarantine bundle
```

Schema 0.3 recordings include stable entity IDs, transaction and undo-entry
IDs, project profile/tool context, compressed content-addressed project states,
capture-time checkpoint proxies, and explicit abort/recovery lifecycle events.
The readers remain backward-compatible with schema 0.1 and 0.2 trajectories
and finalized legacy sample directories.

### Record a session

Build the fork using the instructions in
[`video-path-pilot/README.md`](video-path-pilot/README.md), then launch the
instrumented editor with a fresh absolute JSONL path:

```bash
./video-path-pilot/run-video-path-pilot.sh /absolute/path/session-001.jsonl
```

The launcher configures the Craft runtime, verifies that a usable font is
available, and writes exact state/checkpoint artifacts beside the JSONL. The
recorder stays disabled when `KDENLIVE_VIDEO_PATH_LOG` is not configured, so a
normal Kdenlive launch is unaffected.

### Reconstruct and assemble the dataset

The `edit-path` CLI implements validation, reconstruction, atomic publication,
multi-worker queue claiming, dataset indexing, and deterministic human-QA
sampling:

```bash
python3 -m edit_path doctor
python3 -m edit_path inspect /path/to/finalized-sample
python3 -m edit_path ingest /path/to/finalized-sample /path/to/work-queue
python3 -m edit_path work-one /path/to/work-queue /path/to/dataset \
  --runtime-lock /path/to/runtime-lock.json
python3 -m edit_path qa-sample /path/to/dataset --sample-rate 0.10
python3 -m edit_path index /path/to/dataset
```

Accepted samples contain the final MP4, portable reconstructed Kdenlive
project, cleaned and raw trajectories, assets and manifest, exact states,
checkpoint references, and a render report. Rejected sessions retain their raw
evidence and a machine-readable gate failure under `quarantine/`.

The pinned reconstruction image is defined in
[`reconstruction/Containerfile`](reconstruction/Containerfile). Detailed gate
semantics, worker operation, runtime locking, QA review, and publication bundle
layout are documented in
[`video-path-pilot/RECONSTRUCTION.md`](video-path-pilot/RECONSTRUCTION.md).
Asset licensing metadata is retained but is intentionally non-blocking until
the publication phase; operators can enable that later with
`--require-license`.

For more information about Kdenlive's features, tutorials, and community, please visit our [official website](https://kdenlive.org).

There you can also find downloads for both stable releases and experimental daily builds for Kdenlive.

## Contributing to Kdenlive

Kdenlive is a community-driven project, and we welcome contributions from everyone! There are many ways to contribute beyond coding:

- Help translate Kdenlive into your language
- Report and triage bugs
- Write documentation
- Create tutorials
- Help other users on forums and bug trackers

Visit [kdenlive.org](https://kdenlive.org) to learn more about non-code contributions.

## Developer Information

### Technology Stack

Kdenlive is written in C++ and is using these technologies and frameworks:

- **Core Framework**: MLT for video editing functionality
- **GUI Framework**: Qt and KDE Frameworks 6
- **Additional Libraries**: frei0r (video effects), LADSPA (audio effects)

### Getting Started

1. Check out our [build instructions](dev-docs/build.md) to set up your development environment
2. Familiarize yourself with the [architecture](dev-docs/architecture.md) and [coding guidelines](dev-docs/coding.md)
4. If the MLT library is new to you check out [MLT Introduction](dev-docs/mlt-intro.md)
3. Join our Matrix channel `#kdenlive-dev:kde.org` for developer discussions and support

### Contributing Code

Kdenlive's primary development happens on [KDE Invent](https://invent.kde.org/multimedia/kdenlive). While we maintain a GitHub mirror, all code contributions should be submitted through KDE's GitLab instance. For more information about KDE's development infrastructure, visit the [KDE GitLab documentation](https://community.kde.org/Infrastructure/GitLab).

### Finding Things to Work On

- Browse open issues on [KDE Invent](https://invent.kde.org/multimedia/kdenlive/-/issues), for example those labeled with [First Task](https://invent.kde.org/multimedia/kdenlive/-/issues?label_name%5B%5D=First%20Task)
- Check the [KDE Bug Tracker](https://bugs.kde.org) for reported issues
- Look for issues tagged with "good first issue" or "help wanted"

Need help getting started? Join our Matrix channel `#kdenlive-dev:kde.org` - our community is friendly and always ready to help new contributors!

Please get in touch with us before working on a task, either by commenting in the issue or through our Matrix channel.
