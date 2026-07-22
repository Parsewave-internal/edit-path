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

# Craft installations do not always ship their own fontconfig configuration.
# Pointing FONTCONFIG_FILE at a missing file makes Qt render every label as a
# square, so select the first configuration that actually exists.
fontconfig_file=""
for candidate in \
    "${FONTCONFIG_FILE:-}" \
    "$craft_root/etc/fonts/fonts.conf" \
    /etc/fonts/fonts.conf \
    /usr/etc/fonts/fonts.conf; do
    if [[ -n $candidate && -r $candidate ]]; then
        fontconfig_file=$candidate
        break
    fi
done
if [[ -z $fontconfig_file ]]; then
    echo "error: no readable fontconfig configuration was found" >&2
    exit 1
fi
export FONTCONFIG_FILE=$fontconfig_file
export FONTCONFIG_PATH=$(dirname -- "$fontconfig_file")

# Fail clearly instead of launching an unreadable editor when a minimal
# container has fontconfig but no installed font files.
if command -v fc-match >/dev/null 2>&1; then
    matched_font=$(fc-match --format '%{file}\n' sans-serif 2>/dev/null || true)
    if [[ -z $matched_font || ! -r $matched_font ]]; then
        echo "error: no usable UI font was found; install DejaVu or Liberation fonts" >&2
        exit 1
    fi
fi
export MLT_PREFIX="$craft_root"
export MLT_DATA="$craft_root/share/mlt-7"
export MLT_REPOSITORY="$craft_root/lib/mlt-7"
export QT_DATA_DIRS="$source_root/data${QT_DATA_DIRS:+:$QT_DATA_DIRS}"
export KDENLIVE_VIDEO_PATH_LOG=$log_path
state_dir=${KDENLIVE_VIDEO_PATH_STATE_DIR:-${log_path%.jsonl}-states}
if [[ $state_dir != /* ]]; then
    echo "error: KDENLIVE_VIDEO_PATH_STATE_DIR must be absolute" >&2
    exit 2
fi
log_parent=$(cd -- "$(dirname -- "$log_path")" && pwd)
state_dir=$(realpath -m -- "$state_dir")
state_parent=$(dirname -- "$state_dir")
if [[ $state_parent != "$log_parent" && $state_parent != "$log_parent"/* ]]; then
    echo "error: state sidecars must stay beneath the session log directory: $log_parent" >&2
    exit 2
fi
export KDENLIVE_VIDEO_PATH_STATE_DIR=$state_dir

exec "$binary"
