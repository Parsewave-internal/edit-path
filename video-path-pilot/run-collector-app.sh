#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 Video Path Pilot contributors
# SPDX-License-Identifier: GPL-3.0-only

set -euo pipefail
script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
repo_root=$(cd -- "$script_dir/.." && pwd)
craft_root=${KDENLIVE_PILOT_CRAFT_ROOT:-/home/tenali/CraftRoot}
binary="$repo_root/build/collector-gui/edit-path-collector"
export PATH="$craft_root/dev-utils/bin:$craft_root/bin:$craft_root/libexec:$PATH"

if [[ ! -x $binary ]]; then
    mkdir -p "$repo_root/build/collector-gui"
    cmake -S "$script_dir/gui" -B "$repo_root/build/collector-gui" -GNinja \
        -DCMAKE_PREFIX_PATH="$craft_root"
    cmake --build "$repo_root/build/collector-gui"
fi

export EDIT_PATH_REPO_ROOT="$repo_root"
export LD_LIBRARY_PATH="$craft_root/lib:$craft_root/usr/lib/x86_64-linux-gnu${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
export FONTCONFIG_FILE="$craft_root/etc/fonts/fonts.conf"
export FONTCONFIG_PATH="$craft_root/etc/fonts"
exec "$binary"
