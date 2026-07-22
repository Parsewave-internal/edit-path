#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 Edit Path contributors
# SPDX-License-Identifier: GPL-3.0-only

set -euo pipefail

repo_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
cd "$repo_root"

python3 -m compileall -q edit_path video-path-pilot
python3 -m json.tool video-path-pilot/video-path.schema.json >/dev/null
python3 -m unittest discover -s video-path-pilot/tests -v
python3 -m unittest discover -s tests/edit_path -v

if command -v git >/dev/null 2>&1; then
    git diff --check
fi
