"""Small, opt-in Whisper installer for the portable editor."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path


def _windows_venv() -> tuple[Path, Path] | None:
    if os.name != "nt":
        return None
    root = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "EditPath" / "python"
    return root / "Scripts" / "python.exe", root / "Scripts" / "whisper.exe"


def whisper_binary() -> str | None:
    configured = os.environ.get("EDIT_PATH_WHISPER_BIN")
    if configured:
        return configured
    on_path = shutil.which("whisper")
    if on_path:
        return on_path
    installed = _windows_venv()
    if installed and installed[1].is_file():
        return str(installed[1])
    return None


def whisper_available() -> bool:
    if whisper_binary():
        return True
    try:
        return subprocess.run(
            [sys.executable, "-m", "whisper", "--help"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        ).returncode == 0
    except OSError:
        return False


def install_whisper() -> None:
    """Install Whisper into a reusable venv when the ZIP's Python is embedded.

    Python's Windows embeddable distribution intentionally has no pip.  The
    old installer attempted ``python -m pip --user`` against that interpreter,
    so clicking Yes in the finalization prompt could never produce a transcript.
    Use the first-run dependency venv when present, or create it with the
    system ``py.exe`` launcher, then expose its CLI to the current process.
    """
    existing = _windows_venv()
    if existing:
        venv_python, venv_binary = existing
        venv_python.parent.mkdir(parents=True, exist_ok=True)
        if not venv_python.is_file():
            launcher = shutil.which("py.exe") or shutil.which("python.exe")
            if not launcher:
                raise RuntimeError("Python 3.11 is required to install Whisper on Windows")
            launcher_args = [launcher, "-3.11"] if Path(launcher).name.lower() == "py.exe" else [launcher]
            subprocess.check_call([*launcher_args, "-m", "venv", str(venv_python.parent.parent)])
        subprocess.check_call([str(venv_python), "-m", "pip", "install", "--disable-pip-version-check", "-U", "openai-whisper"])
        if venv_binary.is_file():
            os.environ["EDIT_PATH_WHISPER_BIN"] = str(venv_binary)
    else:
        subprocess.check_call([sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "-U", "openai-whisper"])
    if not whisper_available():
        raise RuntimeError("Whisper installation completed but the provider could not be started")
