# Dataset item layout

Each `completed-sample` is a self-contained dataset item. Its top level is
organized by role rather than by the names used by the collection pipeline.

```text
sample.json
bundle-manifest.json
inputs/
  assets/
outputs/
  final.<container>
edit-path/
  events.jsonl
  replay.mp4
verification/
  reconstructed.mp4
  reconstructed.kdenlive
  report.json
  checkpoints/
provenance/
  assembled-events.jsonl
  segments/
  states/
  editor-project.kdenlive
  asset-bindings.json
  entity-map.json
  session.json
```

## Authoritative training content

`sample.json` is the entry point and authoritative software-independent
training record. It contains the task, input asset metadata, normalized edit
operations, final-output reference, and quality status. `inputs/assets/`
contains the exact media supplied to the editor. `outputs/final.*` is the
editor's target result. These are the fields a dataset loader normally needs.

`edit-path/events.jsonl` retains the validated canonical event trajectory for
sequence-oriented consumers. `edit-path/replay.mp4` is a human-readable visual
explanation of that trajectory; despite being useful for review, it is not the
editor's final output.

## Verification and provenance

`verification/` contains derived QA artifacts. They prove that the normalized
state can reconstruct the editor's result, but are not additional target
outputs. `provenance/` contains native Kdenlive evidence needed for auditing,
debugging, and future re-normalization.

`provenance/asset-bindings.json` is not a second dataset manifest. It records
Kdenlive-native bin identifiers and licensing collection state that do not
belong in the software-independent input description. `bundle-manifest.json`
lists the SHA-256 and size of every packaged file and is refreshed only after
all paths and metadata are final.
