#!/usr/bin/env bash
# Install dependencies without modifying system Python or unrelated installs.
set -euo pipefail
if ! command -v brew >/dev/null 2>&1; then
  echo "Homebrew is required: https://brew.sh" >&2; exit 1
fi
brew install cmake ninja pkg-config python@3.12 ffmpeg zstd mlt qt
repo_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)
python3 -m venv "$repo_root/.venv"
"$repo_root/.venv/bin/python" -m pip install --upgrade pip
"$repo_root/.venv/bin/python" -m pip install -e "$repo_root"
echo "Dependencies installed; build with cmake -S '$repo_root' -B '$repo_root/build' -GNinja"
