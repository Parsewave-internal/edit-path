#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 Video Path Pilot contributors
# SPDX-License-Identifier: GPL-3.0-only

set -euo pipefail

if [[ $# -lt 1 || $# -gt 2 ]]; then
    echo "usage: $0 /absolute/path/session.jsonl [/absolute/path/project.kdenlive]" >&2
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
binary="$source_root/build/bin/kdenlive"
platform=$(uname -s)
if [[ $platform == Darwin && -x $source_root/build/bin/kdenlive.app/Contents/MacOS/kdenlive ]]; then
    binary="$source_root/build/bin/kdenlive.app/Contents/MacOS/kdenlive"
fi
craft_root=${KDENLIVE_PILOT_CRAFT_ROOT:-}

if [[ ! -x $binary ]]; then
    echo "error: pilot binary is missing; build it first: $binary" >&2
    exit 1
fi

if [[ -n $craft_root ]]; then
    if [[ ! -d $craft_root ]]; then
        echo "error: KDENLIVE_PILOT_CRAFT_ROOT does not exist: $craft_root" >&2
        exit 1
    fi
    export PATH="$craft_root/dev-utils/bin:$craft_root/bin:$craft_root/libexec:$PATH"
    if [[ $platform == Darwin ]]; then
        export DYLD_LIBRARY_PATH="$craft_root/lib${DYLD_LIBRARY_PATH:+:$DYLD_LIBRARY_PATH}"
    else
        export LD_LIBRARY_PATH="$craft_root/lib:$craft_root/usr/lib/x86_64-linux-gnu${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
    fi
    export PKG_CONFIG_PATH="$craft_root/lib/pkgconfig:$craft_root/share/pkgconfig${PKG_CONFIG_PATH:+:$PKG_CONFIG_PATH}"
fi

# macOS uses Core Text rather than fontconfig. Linux Craft installations do not
# always ship their own font configuration, so validate it only there.
if [[ $platform != Darwin ]]; then
    fontconfig_file=""
    for candidate in \
        "${FONTCONFIG_FILE:-}" \
        "${craft_root:+$craft_root/etc/fonts/fonts.conf}" \
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

    if command -v fc-match >/dev/null 2>&1; then
        matched_font=$(fc-match --format '%{file}\n' sans-serif 2>/dev/null || true)
        if [[ -z $matched_font || ! -r $matched_font ]]; then
            echo "error: no usable UI font was found; install DejaVu or Liberation fonts" >&2
            exit 1
        fi
    fi
fi

# Kdenlive's timeline QML imports QtMultimedia. A missing runtime module leaves
# the timeline root null and can turn a packaging error into a Qt crash.
qml_import_root=""
if command -v qtpaths6 >/dev/null 2>&1; then
    qml_import_root=$(qtpaths6 --query QT_INSTALL_QML 2>/dev/null || true)
fi
if [[ -z $qml_import_root && -n $craft_root && -d $craft_root/qml ]]; then
    qml_import_root=$craft_root/qml
fi
if [[ -z $qml_import_root || ! -r $qml_import_root/QtMultimedia/qmldir ]]; then
    echo "error: QtMultimedia QML is missing; install the Qt 6 multimedia imports package (qt6-multimedia-imports on openSUSE)" >&2
    exit 1
fi
if [[ -n $craft_root ]]; then
    export MLT_PREFIX="$craft_root"
    [[ -d $craft_root/share/mlt-7 ]] && export MLT_DATA="$craft_root/share/mlt-7"
    [[ -d $craft_root/lib/mlt-7 ]] && export MLT_REPOSITORY="$craft_root/lib/mlt-7"
fi
export QT_DATA_DIRS="$source_root/data${QT_DATA_DIRS:+:$QT_DATA_DIRS}"
export KDENLIVE_VIDEO_PATH_LOG=$log_path
state_dir=${KDENLIVE_VIDEO_PATH_STATE_DIR:-${log_path%.jsonl}-states}
if [[ $state_dir != /* ]]; then
    echo "error: KDENLIVE_VIDEO_PATH_STATE_DIR must be absolute" >&2
    exit 2
fi
log_parent=$(cd -- "$(dirname -- "$log_path")" && pwd)
if ! command -v python3 >/dev/null 2>&1; then
    echo "error: python3 is required to resolve the state-sidecar path" >&2
    exit 1
fi
state_dir=$(python3 -c 'import pathlib, sys; print(pathlib.Path(sys.argv[1]).resolve())' "$state_dir")
state_parent=$(dirname -- "$state_dir")
if [[ $state_parent != "$log_parent" && $state_parent != "$log_parent"/* ]]; then
    echo "error: state sidecars must stay beneath the session log directory: $log_parent" >&2
    exit 2
fi
export KDENLIVE_VIDEO_PATH_STATE_DIR=$state_dir

arguments=()
if [[ -n ${KDENLIVE_VIDEO_PATH_CONFIG:-} ]]; then
    arguments+=(--config "$KDENLIVE_VIDEO_PATH_CONFIG" --no-welcome)
fi
if [[ -n ${KDENLIVE_VIDEO_PATH_CLIPS:-} ]]; then
    arguments+=(-i "$KDENLIVE_VIDEO_PATH_CLIPS")
fi
if [[ $# -eq 2 ]]; then
    arguments+=("$2")
fi

exec "$binary" "${arguments[@]}"
