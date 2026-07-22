#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 Edit Path contributors
# SPDX-License-Identifier: GPL-3.0-only

set -euo pipefail

repo_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
cd "$repo_root"

python3 -m compileall -q edit_path video-path-pilot
python3 -m json.tool video-path-pilot/video-path.schema.json >/dev/null
python3 -m json.tool video-path-pilot/sample.schema.json >/dev/null
python3 -m json.tool video-path-pilot/job.schema.json >/dev/null
python3 -m unittest discover -s video-path-pilot/tests -v
python3 -m unittest discover -s tests/edit_path -v
bash -n video-path-pilot/run-collector-app.sh video-path-pilot/run-video-path-pilot.sh

if ! command -v ffmpeg >/dev/null 2>&1 || ! command -v ffprobe >/dev/null 2>&1 || { ! command -v melt >/dev/null 2>&1 && ! command -v mlt-melt >/dev/null 2>&1; }; then
    if ! command -v docker >/dev/null 2>&1; then
        echo "error: real media validation requires FFmpeg/MLT or Docker" >&2
        exit 1
    fi
    reconstruction_image=${EDIT_PATH_RECONSTRUCTION_TEST_IMAGE:-edit-path-reconstruction:verification}
    docker build -q -t "$reconstruction_image" -f reconstruction/Containerfile . >/dev/null
    docker run --rm --entrypoint python3 \
        -e PYTHONPATH=/src \
        -v "$repo_root:/src:ro" \
        -w /src \
        "$reconstruction_image" \
        -m unittest -v tests.edit_path.test_reconstruction_pipeline.MediaIntegrationTests.test_real_checkpoint_and_final_ssim_pipeline
fi

if command -v git >/dev/null 2>&1; then
    git diff --check
fi
