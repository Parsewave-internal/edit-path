#!/usr/bin/env bash
# Prevent the AppImage deployment helper from bundling the optional CUPS print
# plugin. That plugin is unused by EditPath and requires a newer libcrypt ABI.
set -euo pipefail
if [[ ${1:-} == -query && ${2:-} == QT_INSTALL_PLUGINS ]]; then
    printf '%s\n' "$EDIT_PATH_QT_PLUGIN_ROOT"
    exit 0
fi
if [[ ${1:-} == -query && $# == 1 ]]; then
    "$EDIT_PATH_QMAKE_REAL" "$@" | sed "s|^QT_INSTALL_PLUGINS:.*|QT_INSTALL_PLUGINS:$EDIT_PATH_QT_PLUGIN_ROOT|"
    exit 0
fi
exec "$EDIT_PATH_QMAKE_REAL" "$@"
