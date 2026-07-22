#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-only
set -euo pipefail

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
craft_root=${KDENLIVE_PILOT_CRAFT_ROOT:-/home/tenali/CraftRoot}
build_dir=${EDIT_PATH_BUILD_DIR:-$repo_root/build}
output_dir=${EDIT_PATH_APPIMAGE_OUTPUT_DIR:-$repo_root/linux-output}
linuxdeploy=${LINUXDEPLOY:-$craft_root/dev-utils/bin/linuxdeploy-x86_64.AppImage}
qt_plugin=${LINUXDEPLOY_PLUGIN_QT:-$craft_root/dev-utils/bin/linuxdeploy-plugin-qt-1-alpha-20250213-1-x86_64.AppImage}
appimage_plugin=${LINUXDEPLOY_PLUGIN_APPIMAGE:-$craft_root/dev-utils/bin/linuxdeploy-plugin-appimage-x86_64.AppImage}

for required in "$build_dir/bin/EditPath" "$build_dir/bin/kdenlive" "$craft_root/bin/ffmpeg" \
    "$craft_root/bin/ffprobe" "$craft_root/bin/melt-7" "$craft_root/bin/python3.11" \
    "$linuxdeploy" "$qt_plugin" "$appimage_plugin"; do
    [[ -e "$required" ]] || { echo "Missing required file: $required" >&2; exit 1; }
done

mkdir -p "$output_dir"
work_dir=$(mktemp -d "$output_dir/.appimage-work.XXXXXX")
appdir="$work_dir/EditPath.AppDir"
cleanup() {
    if [[ ${KEEP_APPDIR:-0} == 1 ]]; then
        echo "Kept staging directory: $work_dir"
    else
        rm -rf -- "$work_dir"
    fi
}
trap cleanup EXIT

echo "Staging EditPath in $appdir"
cmake --install "$build_dir" --prefix "$appdir/usr"
install -Dm755 "$craft_root/bin/ffmpeg" "$appdir/usr/bin/ffmpeg"
install -Dm755 "$craft_root/bin/ffprobe" "$appdir/usr/bin/ffprobe"
install -Dm755 "$craft_root/bin/melt-7" "$appdir/usr/bin/melt-7"
ln -s melt-7 "$appdir/usr/bin/melt"
install -Dm755 "$craft_root/bin/python3.11" "$appdir/usr/bin/python3.11"
ln -s python3.11 "$appdir/usr/bin/python3"

mkdir -p "$appdir/usr/lib" "$appdir/usr/share"
cp -a "$craft_root/lib/mlt-7" "$appdir/usr/lib/"
cp -a "$craft_root/share/mlt-7" "$appdir/usr/share/"
cp -a "$craft_root/lib/frei0r-1" "$appdir/usr/lib/"
# Python's pyexpat extension and Craft were built against this newer Expat.
# linuxdeploy normally blacklists Expat as a base-system library, which lets an
# older host copy load and fail with an undefined XML_SetReparseDeferralEnabled.
cp -a "$craft_root/lib"/libexpat.so* "$appdir/usr/lib/"
mkdir -p "$appdir/usr/plugins/kf6/kio"
for kio_worker in "$craft_root/plugins/kf6/kio"/*.so; do
    install -Dm755 "$kio_worker" "$appdir/usr/plugins/kf6/kio/$(basename "$kio_worker")"
done
install -Dm755 "$craft_root/plugins/platforms/libqoffscreen.so" \
    "$appdir/usr/plugins/platforms/libqoffscreen.so"
# Kdenlive's splash screen imports this KDE style from its compiled QML
# resource. qmlimportscanner cannot discover that import from source alone, so
# seed the module explicitly; the Qt deploy plugin follows its dependencies.
mkdir -p "$appdir/usr/qml/org/kde"
cp -a "$craft_root/qml/org/kde/desktop" "$appdir/usr/qml/org/kde/"

# Keep Python's standard library and the one third-party reconstruction module.
cp -a "$craft_root/lib/python3.11" "$appdir/usr/lib/"
find "$appdir/usr/lib/python3.11/site-packages" -mindepth 1 -maxdepth 1 \
    ! -name zstandard ! -name 'zstandard-*.dist-info' -exec rm -rf -- {} +
find "$appdir/usr/lib/python3.11" -type d -name __pycache__ -prune -exec rm -rf -- {} +
# This optional legacy module is unused and depends on libcrypt.so.2, which is
# unavailable on several otherwise supported distributions.
find "$appdir/usr/lib/python3.11/lib-dynload" -name '_crypt*.so' -delete

install -Dm644 "$repo_root/packaging/linux/org.parsewave.EditPath.desktop" \
    "$appdir/usr/share/applications/org.parsewave.EditPath.desktop"
install -Dm644 "$appdir/usr/share/icons/hicolor/512x512/apps/kdenlive.png" \
    "$appdir/usr/share/icons/hicolor/512x512/apps/org.parsewave.EditPath.png"
install -Dm755 "$repo_root/packaging/linux/AppRun" "$appdir/AppRun"

# Expose all Qt plugins except optional printing. The Craft printing plugin is
# linked to libcrypt.so.2 and is neither required by the editor nor portable to
# the Ubuntu baseline used for this MVP.
qt_plugin_root="$work_dir/qt-plugins"
mkdir -p "$qt_plugin_root"
for plugin_group in "$craft_root/plugins"/*; do
    [[ $(basename "$plugin_group") == printsupport ]] && continue
    ln -s "$plugin_group" "$qt_plugin_root/$(basename "$plugin_group")"
done
# The deployment helper expects this module's directory to exist even when it
# intentionally contains no plugins.
mkdir -p "$qt_plugin_root/printsupport"
install -Dm755 "$repo_root/packaging/linux/qmake-wrapper.sh" "$work_dir/qmake-wrapper.sh"

export PATH="$(dirname "$qt_plugin"):$(dirname "$appimage_plugin"):$PATH"
export LD_LIBRARY_PATH="$craft_root/lib:$craft_root/usr/lib/x86_64-linux-gnu${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
export LINUXDEPLOY_PLUGIN_QT="$qt_plugin"
export LINUXDEPLOY_PLUGIN_APPIMAGE="$appimage_plugin"
export EDIT_PATH_QMAKE_REAL="$craft_root/bin/qmake6"
export EDIT_PATH_QT_PLUGIN_ROOT="$qt_plugin_root"
export QMAKE="$work_dir/qmake-wrapper.sh"
export QML_SOURCES_PATHS="$repo_root"
export VERSION=${EDIT_PATH_VERSION:-mvp}
export OUTPUT="$output_dir/EditPath-${VERSION}-x86_64.AppImage"
export APPIMAGE_EXTRACT_AND_RUN=1

rm -f -- "$OUTPUT"
"$linuxdeploy" --appdir "$appdir" \
    --desktop-file "$appdir/usr/share/applications/org.parsewave.EditPath.desktop" \
    --icon-file "$appdir/usr/share/icons/hicolor/512x512/apps/org.parsewave.EditPath.png" \
    --executable "$appdir/usr/bin/EditPath" \
    --executable "$appdir/usr/bin/kdenlive" \
    --executable "$appdir/usr/bin/ffmpeg" \
    --executable "$appdir/usr/bin/ffprobe" \
    --executable "$appdir/usr/bin/melt-7" \
    --executable "$appdir/usr/bin/python3.11" \
    --custom-apprun "$repo_root/packaging/linux/AppRun" \
    --plugin qt --output appimage

chmod +x "$OUTPUT"
sha256sum "$OUTPUT" >"$OUTPUT.sha256"
echo "Built: $OUTPUT"
echo "Checksum: $OUTPUT.sha256"
