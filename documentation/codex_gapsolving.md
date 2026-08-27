<!--
SPDX-FileCopyrightText: 2026 Video Path Pilot contributors
SPDX-License-Identifier: GPL-3.0-only
-->

# Gap 2 — sidecars and the asset identity map

Client report, two parts:

1. **Sidecars didn't ship.** The TikTok delivery references 2,373
   `states/*.kdenlive.zst` files and its `session.end` says
   `state_sidecars_complete: false`. Without the sidecars there is no final
   project state in the record, only the rendered MP4, so there is nothing to
   verify a reconstruction against.
2. **No asset identity map.** Neither delivery maps
   `asset_reference`/`asset_id` to an input filename. In The Signal, none of the
   39 input filenames appear in the record and there are 45 distinct asset
   references, so they cannot be resolved even by guessing order.

Both are packaging problems, not design problems: the recorder already emits the
identity information and the gates already validate sidecars. The loss happened
downstream, in what got written into the delivered bundle. Each fix below is
tied to the specific line that caused the loss.

## Diagnosis

### Why the sidecars went missing

`publish_bundle()` writes three event logs into a bundle, and only one of them
was ever made portable:

| Log | How it is written | Sidecars copied? |
| --- | --- | --- |
| `trajectory.jsonl` | `_clean_events()` then `_make_state_references_portable()` | yes |
| `raw-trajectory.jsonl` | `shutil.copy2`, byte-for-byte | **no** |
| `evidence/raw-events-*.jsonl` | `shutil.copy2`, byte-for-byte | **no** |

`_clean_events()` (`edit_path/pipeline.py:338`) keeps only accepted commits and
checkpoints. Every undone, redone, or rejected edit is dropped. Those dropped
events still carry `project_state` sidecar references, and they survive verbatim
in `raw-trajectory.jsonl` — which keeps its original session-relative
`states/...` paths pointing at files that were never copied into the bundle.

That is the 2,373 figure. A long session accumulates thousands of exact states;
only the accepted subset was rehomed, and the rest stayed as dangling paths.
Nothing in the pipeline checked this, so the bundle published cleanly.

The `state_sidecars_complete: false` observation is a separate signal, and the
gate for it already works (`edit_path/pipeline.py:62`, verified below). A
session that ends that way is rejected at ingestion today. So the reported
bundle either predates that gate or bypassed `process_session()`. Worth
confirming against the delivery's provenance before assuming more code is
needed — I did not change that gate.

### Why the asset map was unresolvable

The recorder writes two unrelated identifiers onto every clip
(`src/videopath/videopathrecorder.cpp:443-444`):

```cpp
clip.insert(QStringLiteral("asset_reference"), assetReference);  // Kdenlive bin ID, e.g. "4"
clip.insert(QStringLiteral("asset_id"), stableEntityId(QStringLiteral("asset"), assetReference));
```

`stableEntityId()` returns a fresh `QUuid` (`videopathrecorder.cpp:312-323`).
It is **not** a manifest asset ID — manifest IDs are `asset_001`-style, minted
by `prepare_assets()`. The two identity spaces are unrelated, and the only
reliable link between a bin reference and an input file is `bindings`, which
`prepare_assets()` derives from each asset's SHA-256 rather than from import
order.

An earlier attempt resolved with `value.get("asset_id") or bindings.get(reference)`.
Because the recorder always populates `asset_id`, the UUID always won, never
matched the manifest, and both publication sites were guarded by
`if asset_id in by_id` — so both silently emitted nothing. The map was empty on
every real recording and only populated for legacy events that omit `asset_id`,
the opposite of the reported deliveries. Reproduced before the fix:

```
reference_to_asset: {'4': '3f2c9b10-...', '5': 'b81de4aa-...'}
published asset_references map: {}
```

Two further gaps in that approach: it scanned only `state.diff` events, missing
every clip that existed at the baseline checkpoint and was never edited
afterwards; and it published `"file"` into the binding index, which regressed
re-ingestion (see below).

## Changes

Five changes, each addressing one of the above.

### 1. Rehome sidecars in verbatim logs — `edit_path/pipeline.py`

`_rehome_verbatim_state_references()` walks `raw-trajectory.jsonl` and each
evidence segment, copies every sidecar that still exists into `states/` under
its content address, and repoints the reference at the copy. This reuses the
content-addressed naming `_make_state_references_portable()` already uses, so
states shared between the cleaned and verbatim logs deduplicate to one file.

A sidecar the recorder never durably wrote is marked `"available": false`
instead of being left as a dangling path. This distinction matters: a consumer
can tell "the recorder lost this state" from "the packaging lost this state".
Silently dropping the reference would erase evidence that an edit happened.

### 2. Refuse to publish a bundle with dangling references — `edit_path/pipeline.py`

`_verify_no_dangling_state_references()` runs over all three delivered logs
after rehoming and raises `GateError("state_sidecars", ...)` if any event names
a sidecar the bundle does not contain. References explicitly marked
`available: false` are tolerated.

This is the gate the reported deliveries needed. A bundle whose record points at
states it does not carry cannot verify a reconstruction, so it must not be
publishable. Without this the class of bug recurs the next time an event log is
added to the bundle.

### 3. Name the final project state — `edit_path/pipeline.py`

`publish_bundle()` now writes `final-project-state.json` identifying the last
exact state in the record by path, sha256, bytes and encoding. This addresses
"there's nothing to verify a reconstruction against" directly: the state was
usually present in the bundle, but a consumer had to replay the whole event
chain to work out which of thousands of sidecars was the final one.
`organize_dataset_item()` moves it to `verification/`, next to the artifacts it
is used to check.

### 4. Resolve references through hash bindings — `video-path-pilot/job_pipeline.py`

`asset_reference_index()` replaces the broken inline block. It resolves through
`bindings` (SHA-256 derived, order-independent) and publishes, per reference:

- `resolution` — `file` or `embedded`
- `asset_id`, `original_filename`, `sha256` for file-backed references
- `recorder_asset_uuid` — the recorder's UUID, preserved in its own field so the
  two identity spaces stay separate rather than being conflated
- embedded metadata for titles, colour clips and nested sequences, which have no
  input filename because the project XML carries them entirely

`observed_asset_values()` scans **both** `state.checkpoint` snapshots and
`state.diff` changes, so clips present at the baseline and never edited are
included. `used_asset_references()` now shares this scan, so the packaging gate
and the published map cannot disagree about which references an edit used.

The result is published in two places: `asset-manifest.json` (as
`asset_references`, schema `video-path/assets@3`) and, after organization,
`provenance/asset-bindings.json` (schema `video-path/native-asset-bindings@2`).
Each asset also gains an `asset_references` list for the reverse direction.

### 5. Keep the binding index from clobbering asset paths — `video-path-pilot/job_pipeline.py`

The binding index deliberately publishes `original_filename`, `sha256` and
`bytes` but **not** `file`. `load_manifest()` overlays binding keys onto
`sample.json`, and the two disagree on prefix (`assets/` vs `inputs/assets/`).
Republishing the pre-organization path made the stale one win and broke asset
verification on re-ingest:

```
merged file field -> assets/abc-clip.mp4
verify_assets FAILED: GateError asset 0 is missing: assets/abc-clip.mp4
```

`sample.json` owns the organized path; the binding index adds identity only.

## Verification

Run with Python 3.12 in a venv with `zstandard` (needed to exercise the sidecar
path rather than skip it). Note `python3` on this machine is 3.8 and cannot
parse the `X | None` annotations in `edit_path/errors.py`; the resulting
`TypeError: unsupported operand type(s) for |` is environmental, not a defect.

```bash
python3.12 -m venv /tmp/epvenv && /tmp/epvenv/bin/pip install zstandard

/tmp/epvenv/bin/python -m unittest \
    tests.edit_path.test_reconstruction_pipeline tests.edit_path.test_segments
# → Ran 32 tests, OK (skipped=3: ffmpeg/MLT and two absent pilot fixtures)

cd video-path-pilot && /tmp/epvenv/bin/python -m unittest discover -s tests
# → Ran 21 tests, OK
```

`ffmpeg`/`melt` are not installed here, so tests needing a real render stay
skipped. The changed code is all packaging logic and is fully covered without
them.

### Tests added

In `tests/edit_path/test_reconstruction_pipeline.py`:

- `test_verbatim_logs_get_their_undone_state_sidecars_copied` — an undone edit's
  sidecar is copied and repointed; a never-written one is marked
  `available: false`; the gate tolerates the marker and rejects a real dangle.
- `test_published_bundle_carries_every_state_it_references` — end-to-end
  packaging of the reported failure. A session with one accepted and one undone
  edit goes through `publish_bundle()` and `organize_dataset_item()`; asserts
  both states ship, the verbatim log points at the copy, the final state is
  named, and every reference still resolves after the role-oriented layout move.

In `video-path-pilot/tests/test_mvp.py`:

- `test_asset_references_resolve_through_hash_bindings_not_recorder_uuids` —
  the exact lossy scenario: clips carrying both keys, where the recorder UUID
  previously shadowed the binding and produced an empty map.
- `test_asset_references_survive_more_references_than_files` — the reported
  45-references-over-39-files shape: two bin IDs sharing one input file, plus an
  embedded title with no input file.
- `test_asset_references_include_untouched_baseline_clips` — a clip present at
  the baseline and never edited still resolves.
- `test_completed_sample_has_role_oriented_layout` — updated; it asserted the
  old lossy binding shape, and now pins the `@2` shape including the absence of
  `file`.

### The new gate provably catches the reported bug

Disabling the rehoming call and rerunning the end-to-end test:

```
GateError: published bundle references a missing state sidecar:
  states/c5fe87b4...cd3dc.kdenlive.zst (raw-trajectory.jsonl)
FAILED (errors=1)
```

Restored, it passes. The test fails for the right reason rather than passing
vacuously.

### Behaviour confirmed by direct probe

Reference resolution on a realistic recorder event, which produced `{}` before:

```json
{
  "4": {
    "recorder_asset_uuid": "3f2c9b10-7a41-4d2e-9c55-8ab0d1e6f7a9",
    "resolution": "file",
    "asset_id": "asset_001",
    "original_filename": "clip_a.mp4",
    "sha256": "aaaa…"
  },
  "5": { "…": "clip_b.mp4" }
}
```

Re-ingestion of an organized bundle, which failed before change 5:

```
merged file field -> inputs/assets/abc-clip.mp4
verify_assets: OK
```

The pre-existing `state_sidecars_complete` gate, unchanged:

```
state_sidecars_complete=False -> REJECTED [state_sidecars] v0.3 session did not
                                 durably finish its state and checkpoint sidecars
state_sidecars_complete=True  -> ACCEPTED
```

## What this does not cover

- **Existing deliveries are not repaired.** These changes fix the packaging path
  going forward. The TikTok and Signal bundles must be repackaged from their
  source sessions; if those sessions are gone, the undone-edit states are
  unrecoverable.
- **`state_sidecars_complete: false` is not newly handled.** That gate already
  rejects such sessions. The reported bundle predates it or bypassed
  `process_session()`, which is worth confirming from its provenance.
- **No recorder changes.** The identity data the recorder emits is sufficient;
  only the packaging discarded it. Making `stableEntityId()` agree with manifest
  IDs would be a design change, not a packaging fix, and the SHA-256 bindings
  are the more reliable link regardless.
