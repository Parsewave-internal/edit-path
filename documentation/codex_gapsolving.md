<!--
SPDX-FileCopyrightText: 2026 Video Path Pilot contributors
SPDX-License-Identifier: GPL-3.0-only
-->

# Gap 2 — sidecars and the asset identity map: status of the in-progress fix

This file documents the uncommitted change in `video-path-pilot/job_pipeline.py`
that was written to close client gap 2, what that change actually does, how it
flows through the pipeline, and which parts of the gap are still open. It is an
assessment document, not a claim that the gap is closed.

## The gap as reported

The client raised two distinct complaints under one heading.

1. **Sidecars didn't ship.** The TikTok delivery referenced 2,373
   `states/*.kdenlive.zst` files and its `session.end` carried
   `state_sidecars_complete: false`, so there was no final project state in the
   record to verify a reconstruction against — only the rendered MP4.
2. **No asset identity map.** Neither delivery contained a map from
   `asset_reference` → `asset_id` → input filename. In The Signal, none of the
   39 input filenames appeared in the record while 45 distinct asset references
   did, so references could not be resolved even by guessing import order.

Both were characterised as packaging problems rather than design problems. That
framing is right: the recorder already emits the identity information, and the
reconstruction gates already validate sidecars. The loss happened downstream, in
what got written into the delivered bundle.

## What the change does

The diff touches `video-path-pilot/job_pipeline.py` in three places.

### 1. The published binding index keeps source identity

`organize_dataset_item()` moves `asset-manifest.json` to
`provenance/asset-bindings.json` and rewrites it. Previously the rewrite kept
only `asset_id`, `bin_reference`/`bin_references` and `license_status`, so the
delivered file was a pure Kdenlive-ID index with no way back to a filename. The
rewrite now also carries `original_filename`, `file`, `sha256`, `bytes` and
`asset_references`, and the schema tag moves to
`video-path/native-asset-bindings@2` (`job_pipeline.py:432-444`).

This is the part of the change that does real work. A consumer holding only
`provenance/asset-bindings.json` can now resolve a Kdenlive bin ID to an input
filename and content hash.

### 2. A reference → asset map is collected during finalization

`finalize_session()` walks every `state.diff` event in every recording segment,
picks up each `asset_reference` seen on either side of a change, resolves it to
an asset ID, and records the result in `reference_to_asset`
(`job_pipeline.py:475-497`). Resolution prefers an `asset_id` carried inline on
the change and falls back to `bindings`, the sha256-derived map produced by
`prepare_assets()`. Each manifest asset then gains a sorted
`asset_references` list.

The intent is that references resolve from observed event data rather than from
import order, so an edit with more references than files, duplicate references,
or reordered assets stays resolvable.

### 3. The map is published on the manifest

`asset-manifest.json` gains a top-level `asset_references` object mapping each
reference to `{asset_id, original_filename}`, and the schema tag moves to
`video-path/assets@3` (`job_pipeline.py:529-538`).

### How it reaches the delivered bundle

The plumbing is sound. `finalize_session()` writes
`asset-manifest.json` into the session directory, `load_manifest()`
(`edit_path/assets.py:98`) picks that file up as its first candidate,
`_public_asset_manifest()` (`edit_path/pipeline.py:417`) deep-copies the whole
manifest — stripping only the local `source` and `original_path` keys — and
`publish_bundle()` writes it into the bundle. `organize_dataset_item()` then
moves it to `provenance/asset-bindings.json` and forwards `asset_references`
through the rewrite. Nothing along that path drops the new field.

## Verification performed

Tests run with Python 3.12 (`python3` on this machine is 3.8 and cannot parse
the `X | None` annotations in `edit_path/errors.py`, which is the sole cause of
the `TypeError: unsupported operand type(s) for |` import failure seen earlier —
not a defect in the change).

```
python3.12 -m unittest tests.edit_path.test_reconstruction_pipeline tests.edit_path.test_segments
  → 30 tests, OK (4 skipped: ffmpeg/MLT, zstandard, two pilot fixtures absent)

cd video-path-pilot && python3.12 -m unittest discover -s tests
  → 18 tests, 1 FAILED
```

The reconstruction suite is unaffected. The pilot suite has one failure and I
found two further problems by direct probe.

## What is still open

### A. `test_completed_sample_has_role_oriented_layout` fails

`video-path-pilot/tests/test_mvp.py:48` asserts the rewritten binding index
equals exactly `[{"asset_id": "asset_001", "bin_references": ["4"]}]`. The
richer binding now also carries `file`, `sha256` and `bytes`, so the assertion
fails. This is the test encoding the old lossy shape; the assertion needs
updating to the intended `@2` shape. It is not evidence the new shape is wrong,
but the suite is red and no test yet covers the lossy packaging scenario the
change is meant to fix.

### B. The reference map is empty on real v0.3 recordings

This is the substantive problem. The recorder emits *both* keys on every clip
(`src/videopath/videopathrecorder.cpp:443-444`):

```cpp
clip.insert(QStringLiteral("asset_reference"), assetReference);          // Kdenlive bin ID, e.g. "4"
clip.insert(QStringLiteral("asset_id"), stableEntityId(QStringLiteral("asset"), assetReference));
```

`stableEntityId()` returns a `QUuid` (`videopathrecorder.cpp:312-323`), not a
manifest asset ID. The manifest IDs are `asset_001`-style, minted by
`prepare_assets()` (`job_pipeline.py:345`). Because the resolution order is
`value.get("asset_id") or bindings.get(reference)`, the inline recorder UUID
always wins, and the resulting asset ID is never a key in `by_id`. Both
publication sites are guarded by `if asset_id in by_id`, so both silently emit
nothing.

Reproduced against a realistic recorder event carrying both keys:

```
reference_to_asset: {'4': '3f2c9b10-...-8ab0d1e6f7a9', '5': 'b81de4aa-...-11c9e0a5f3b2'}
published asset_references map: {}
per-asset asset_references: {'asset_001': [], 'asset_002': []}
```

The map populates only for legacy recordings that omit `asset_id` and therefore
fall through to `bindings`. That is exactly backwards from the deliveries the
client is complaining about. The fix is to prefer `bindings.get(reference)` —
the sha256-derived binding, which is the authoritative link to a manifest
asset — and to treat the recorder UUID as a separate stable-identity field
rather than as a manifest asset ID. `normalize_sample.py:181` already keeps the
two spaces distinct via `observed_asset_ids()`; `finalize_session()` conflates
them.

### C. The new `file` key breaks re-ingestion of a delivered item

`load_manifest()` has a recovery path for bundles that no longer have a
top-level `asset-manifest.json`: it reads `sample.json`, then overlays every
non-`asset_id` key from `provenance/asset-bindings.json` on top
(`edit_path/assets.py:121-127`). `sample.json` stores the organized path
`inputs/assets/...`, because `finalize_session()` rewrites it
(`job_pipeline.py:560-563`). The binding index stores the pre-organization
`assets/...` path. The overlay therefore clobbers the correct path with the
stale one:

```
merged file field -> assets/abc-clip.mp4
verify_assets FAILED: GateError asset 0 is missing: assets/abc-clip.mp4
```

So re-verifying a delivered dataset item now fails a gate that passed before the
change. Either omit `file` from the binding index (`original_filename`, `sha256`
and `bytes` carry the identity without the path ambiguity), rewrite it to the
organized prefix alongside the other path rewrites, or exclude `file` from the
overlay in `load_manifest()`.

### D. Complaint 1 — sidecars — is untouched

The diff contains no sidecar changes. The existing machinery is already
strict, and worth stating plainly so it isn't re-litigated:

- `validate_event_envelope()` rejects any complete v0.3 session whose
  `session.end` lacks `state_sidecars_complete: true`
  (`edit_path/pipeline.py:62-63`). A delivery carrying `false` could not pass
  this gate, which suggests the TikTok bundle predates the gate or bypassed
  `process_session()`. Worth confirming against that bundle's provenance before
  concluding anything about current behaviour.
- `validate_project_state_sidecars()` requires an exact `project_state` on every
  v0.3 state event, loads and hash-checks each one, and validates the
  `project_before_hash` → `project_after_hash` chain
  (`edit_path/pipeline.py:111-157`).
- `_make_state_references_portable()` copies each sidecar to
  `states/<sha256><suffix>` inside the bundle and rewrites event paths to that
  content-addressed location, raising `GateError` on a missing or symlinked
  sidecar (`edit_path/pipeline.py:371-392`).
- `organize_dataset_item()` rewrites those paths to `provenance/states/`
  (`job_pipeline.py:450-461`), and the final project ships as
  `provenance/editor-project.kdenlive`.

The `test_zstd_sidecar_hash_and_size_are_checked` test that would exercise the
hash/size path is **skipped** here because `zstandard` is not installed, so the
sidecar validation path is unverified on this machine. Installing `zstandard`
before drawing conclusions about the sidecar half is worthwhile.

### E. Worth checking before more work on the identity map

The claim that filenames are absent from the record deserves a second look,
because `sample.json` — which `documentation.md` calls authoritative — already
carries the full chain. `normalize_sample.py:109-111` rewrites each change's
`asset_reference` into an `asset_id` using `native_asset_bindings`, and
`inputs.assets` carries `asset_id` → `original_filename`
(`normalize_sample.py:220-221`). So `asset_reference` → `asset_id` →
`original_filename` was resolvable from `sample.json` alone before this change.
That suggests the affected deliveries either predate this normalization or were
produced by a different path (`sample_collector.py` writes
`video-path/assets@2` directly at line 235). Establishing which would tell us
whether the remaining work is a packaging fix or a re-delivery.

## Summary

| Item | State |
| --- | --- |
| Binding index keeps `original_filename`/`sha256`/`bytes` | Working |
| `asset_references` plumbed through publish and organize | Working |
| `asset_references` populated for real v0.3 recordings | **Broken** — always empty (B) |
| Re-ingestion of a delivered item | **Regressed** — `verify_assets` gate fails (C) |
| Pilot test suite | **Red** — one stale assertion (A) |
| Test covering the lossy packaging scenario | Not written |
| Sidecar half of the gap | Unchanged; existing gates look sufficient, unverified locally (D) |
| `documentation/codex_gapsolving.md` | Was reported as written but was absent from disk; this file replaces it |
