# Building the Windows portable MVP

This produces an unsigned, portable 64-bit Windows engineering build. It does
not modify the official Kdenlive installation and does not require the editor
to install Python.

## Requirements

- 64-bit Windows 10 or Windows 11;
- at least 40 GB free disk space and 8 GB RAM (16 GB preferred);
- a stable internet connection and several hours for the first build;
- [Git for Windows](https://git-scm.com/download/win);
- [Python 3.11 or newer, 64-bit](https://www.python.org/downloads/windows/),
  with **Add Python to PATH** selected;
- [Visual Studio 2022 Build Tools](https://aka.ms/vs/17/release/vs_BuildTools.exe)
  with **Desktop development with C++** selected.

Visual Studio 2026 is not currently supported by KDE Craft. The build script
temporarily removes conflicting MinGW tools from its own `PATH`, so no manual
environment cleanup is needed.

Administrator access is useful for installing prerequisites, but the resulting
portable application does not require administrator access.

## Build

Use the current `main` branch. A fresh short-path checkout is preferred:

```powershell
cd C:\
mkdir src -ErrorAction SilentlyContinue
cd C:\src
git clone --branch main --single-branch https://github.com/Parsewave-internal/edit-path.git
cd edit-path
```

For an existing checkout, run `git switch main` and `git pull origin main`
before building.

Run the fast prerequisite check first. It does not download or compile Kdenlive:

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\packaging\windows\build-editpath.ps1 -PreflightOnly
```

Only continue when it prints `PREFLIGHT PASSED`.

Open PowerShell in the repository root and run:

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\packaging\windows\build-editpath.ps1
```

Prefer a short checkout path such as `C:\src\edit-path`; long or space-heavy
paths can cause problems in Windows C++ dependency builds.

The script verifies prerequisites before downloading or compiling anything. It
then bootstraps KDE Craft under `C:\CraftRoot`, builds this exact checkout,
packages all runtime dependencies, embeds Python, generates synthetic test
media, verifies both application executables, runs the reconstruction runtime
doctor, and executes the real-media exact-state reconstruction integration
test before creating the archive.
It also prevents sleep while its process is running, writes the complete output
to `windows-output\windows-build.log`, and runs the packaged applications'
non-interactive version/self-tests before creating the ZIP.

The first build may take several hours. Keep PowerShell open and prevent the
computer from sleeping. A failed build can normally be retried with the same
command; Craft reuses completed dependencies.

## Result

Successful output is written to:

```text
windows-output\
├── EditPath-Windows-x64\
├── EditPath-Windows-x64.zip
└── build-manifest.json
```

Run `windows-output\EditPath-Windows-x64\bin\EditPath.exe`. Do not run
`kdenlive.exe` directly because that bypasses recording. Windows SmartScreen
may warn because the MVP has not yet been code-signed; use **More info → Run
anyway** only for an artifact built from the company repository.

If the script fails, save the complete PowerShell output and send the last 100
lines of `windows-output\windows-build.log` along with
`windows-output\build-manifest.json` if it exists. Do not
delete `C:\CraftRoot`, because it contains reusable dependency builds.

The GitHub workflow is a convenience wrapper around the same script. If a job
fails with zero executed steps and no assigned runner, check the organization's
Actions billing/spending limit; that condition is not a compiler or application
failure. A local PowerShell build remains the supported fallback.
