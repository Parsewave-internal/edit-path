# Windows dependency installer

`dependency-installer.ps1` performs the portable dependency gates before an
editor can use local Whisper: x64 architecture, free space, Python/venv,
Whisper installation/import, FFmpeg, DirectShow enumeration, and a generated
one-second audio transcription self-test. It writes a JSON report and exits
non-zero on any failed gate.

```powershell
Set-ExecutionPolicy -Scope Process Bypass -Force
.\dependency-installer.ps1 -Model turbo -InstallRoot "$env:LOCALAPPDATA\EditPath"
```

The final portable Windows build should package this script or wrap it with a
small signed launcher. Whisper model files are downloaded and verified during
installation instead of bloating every EditPath ZIP.
