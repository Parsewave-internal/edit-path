"""Small, opt-in Whisper installer for the portable editor."""
from __future__ import annotations
import os, shutil, subprocess, sys
from pathlib import Path

def whisper_available() -> bool:
    binary = os.environ.get("EDIT_PATH_WHISPER_BIN") or shutil.which("whisper")
    if binary:
        return True
    try:
        return subprocess.run([sys.executable, "-m", "whisper", "--help"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0
    except OSError:
        return False

def install_whisper() -> None:
    root = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "EditPath" / "whisper"
    root.mkdir(parents=True, exist_ok=True)
    subprocess.check_call([sys.executable, "-m", "pip", "install", "--user", "openai-whisper"])
    if not whisper_available():
        raise RuntimeError("Whisper installation completed but the provider could not be started")
