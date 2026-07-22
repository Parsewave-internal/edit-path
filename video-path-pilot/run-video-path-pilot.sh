#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 Video Path Pilot contributors
# SPDX-License-Identifier: GPL-3.0-only

set -euo pipefail

if [[ $# -ne 1 ]]; then
    echo "usage: $0 /absolute/path/session.jsonl" >&2
    exit 2
fi

log_path=$1
if [[ $log_path != /* ]]; then
    echo "error: the session log path must be absolute" >&2
    exit 2
fi
if [[ -e $log_path ]]; then
    echo "error: refusing to append a new session to existing file: $log_path" >&2
    exit 2
fi

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
source_root=$(cd -- "$script_dir/.." && pwd)
craft_root=${KDENLIVE_PILOT_CRAFT_ROOT:-/home/tenali/CraftRoot}
binary="$source_root/build/bin/kdenlive"

if [[ ! -x $binary ]]; then
    echo "error: pilot binary is missing; build it first: $binary" >&2
    exit 1
fi

export PATH="$craft_root/dev-utils/bin:$craft_root/bin:$craft_root/libexec:$PATH"
export LD_LIBRARY_PATH="$craft_root/lib:$craft_root/usr/lib/x86_64-linux-gnu${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
export PKG_CONFIG_PATH="$craft_root/lib/pkgconfig:$craft_root/share/pkgconfig${PKG_CONFIG_PATH:+:$PKG_CONFIG_PATH}"
export FONTCONFIG_FILE="$craft_root/etc/fonts/fonts.conf"
export FONTCONFIG_PATH="$craft_root/etc/fonts"
export MLT_PREFIX="$craft_root"
export MLT_DATA="$craft_root/share/mlt-7"
export MLT_REPOSITORY="$craft_root/lib/mlt-7"
export QT_DATA_DIRS="$source_root/data${QT_DATA_DIRS:+:$QT_DATA_DIRS}"
export KDENLIVE_VIDEO_PATH_LOG=$log_path

arguments=()
if [[ -n ${KDENLIVE_VIDEO_PATH_CONFIG:-} ]]; then
    arguments+=(--config "$KDENLIVE_VIDEO_PATH_CONFIG" --no-welcome)
fi
if [[ -n ${KDENLIVE_VIDEO_PATH_CLIPS:-} ]]; then
    arguments+=(-i "$KDENLIVE_VIDEO_PATH_CLIPS")
fi

exec "$binary" "${arguments[@]}"
