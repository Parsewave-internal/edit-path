# Linux AppImage

The AppImage packages the EditPath Recorder, modified Kdenlive, FFmpeg/FFprobe,
MLT, Python, and the sample-generation pipeline. Editors do not need a terminal
or a separate Kdenlive installation.

## Build

Build on the oldest supported x86_64 Linux distribution whenever possible.
After compiling EditPath and installing the Craft dependencies:

```bash
source /home/tenali/CraftRoot/craft/craftenv.sh
cd /home/tenali/parsewave/edit-path-publish
export KDENLIVE_PILOT_CRAFT_ROOT=/home/tenali/CraftRoot
packaging/linux/build-appimage.sh
```

The AppImage and its SHA-256 checksum are written to `linux-output/`. Qt's
offscreen backend is included so the self-test also works on a headless builder.
KDE's `org.kde.desktop` QML style is seeded explicitly because Kdenlive imports
it from a compiled splash-screen resource that automatic source scanning cannot
see.
The package also includes KDE KIO workers for local file browsing and the
Frei0r runtime effects, with their lookup paths established by `AppRun`.

## Test

Copy it to the GUI laptop, then run:

```bash
chmod +x EditPath-mvp-x86_64.AppImage
./EditPath-mvp-x86_64.AppImage --self-test
./EditPath-mvp-x86_64.AppImage
```

If FUSE is unavailable, prefix either command with
`APPIMAGE_EXTRACT_AND_RUN=1`. The self-test must report `"passed": true`
before an editor session is attempted.

For MVP acceptance, create a session, import media, edit and save the project,
render the final video into the session folder, finish the session, and confirm
that both sample generation and reconstructed media pass.
