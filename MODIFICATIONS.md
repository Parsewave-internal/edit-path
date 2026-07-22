<!--
SPDX-FileCopyrightText: 2026 Video Path Pilot contributors
SPDX-License-Identifier: GPL-3.0-only
-->

# Modified Kdenlive build

This source tree is a modified version of Kdenlive. It is based on upstream
revision `7de2ed9902b4288797a7781498546389a482a39e` and was modified on
2026-07-21.

The modifications add an opt-in Video Path JSONL recorder, semantic timeline
hooks, deterministic transaction-aware undo/redo capture, stable entity IDs,
content-addressed exact project states, capture-time checkpoint references,
project/runtime provenance, and the reconstruction/validation/dataset pipeline
under `edit_path/`. See `video-path-pilot/README.md` and
`video-path-pilot/RECONSTRUCTION.md` for activation and operations.

This modified application is provided under GNU GPL version 3. It is not an
official Kdenlive release, and the upstream Kdenlive project is not responsible
for supporting these changes.
