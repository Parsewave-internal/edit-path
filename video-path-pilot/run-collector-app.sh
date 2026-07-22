#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 Video Path Pilot contributors
# SPDX-License-Identifier: GPL-3.0-only

set -euo pipefail
# Forced Qt/GL software backends caused severe Kdenlive UI stalls over
# forwarded X11 during acceptance testing. The editor chooses its normal Mesa
# fallback without these overrides.
unset QSG_RHI_BACKEND LIBGL_ALWAYS_SOFTWARE
script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
repo_root=$(cd -- "$script_dir/.." && pwd)
binary="$repo_root/build/collector-gui/EditPath"
platform=$(uname -s)
craft_root=${KDENLIVE_PILOT_CRAFT_ROOT:-}
if [[ -n $craft_root && ! -d $craft_root ]]; then
    echo "error: KDENLIVE_PILOT_CRAFT_ROOT does not exist: $craft_root" >&2
    exit 1
fi
cmake_arguments=()
if [[ -n $craft_root ]]; then
    export PATH="$craft_root/dev-utils/bin:$craft_root/bin:$craft_root/libexec:$PATH"
    cmake_arguments+=("-DCMAKE_PREFIX_PATH=$craft_root")
fi

if [[ ! -f $repo_root/build/collector-gui/build.ninja ]]; then
    mkdir -p "$repo_root/build/collector-gui"
    cmake -S "$script_dir/gui" -B "$repo_root/build/collector-gui" -GNinja "${cmake_arguments[@]}"
fi
cmake --build "$repo_root/build/collector-gui"

export EDIT_PATH_REPO_ROOT="$repo_root"
if [[ -n $craft_root ]]; then
    if [[ $platform == Darwin ]]; then
        export DYLD_LIBRARY_PATH="$craft_root/lib${DYLD_LIBRARY_PATH:+:$DYLD_LIBRARY_PATH}"
    else
        export LD_LIBRARY_PATH="$craft_root/lib:$craft_root/usr/lib/x86_64-linux-gnu${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
        [[ -r $craft_root/etc/fonts/fonts.conf ]] && export FONTCONFIG_FILE="$craft_root/etc/fonts/fonts.conf"
        [[ -d $craft_root/etc/fonts ]] && export FONTCONFIG_PATH="$craft_root/etc/fonts"
    fi
    [[ -d $craft_root/share/mlt-7 ]] && export MLT_DATA="$craft_root/share/mlt-7"
    [[ -d $craft_root/lib/mlt-7 ]] && export MLT_REPOSITORY="$craft_root/lib/mlt-7"
    export MLT_PREFIX="$craft_root"
fi
restart_count=0
while true; do
    # Keep the launcher outside the GUI process session on Linux. If the
    # desktop force-quits the editor/recorder application group, this small
    # watchdog survives long enough to reopen the persisted recovery screen.
    if [[ $platform == Linux ]] && command -v setsid >/dev/null 2>&1; then
        if setsid --wait "$binary"; then
            exit 0
        else
            exit_code=$?
        fi
    elif "$binary"; then
        exit 0
    else
        exit_code=$?
    fi

    restart_count=$((restart_count + 1))
    if (( restart_count > 3 )); then
        echo "error: EditPath exited unexpectedly too many times (last exit: $exit_code)" >&2
        exit "$exit_code"
    fi
    echo "EditPath exited unexpectedly (code $exit_code); reopening recovery in 2 seconds…" >&2
    sleep 2
done
