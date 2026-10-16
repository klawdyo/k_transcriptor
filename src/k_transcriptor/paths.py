r"""Where k_transcriptor keeps its own files (its cache), independent of any other project.

  <home>/transcripts/<video id>.json   the transcripts (the cache: a video already here is never transcribed again)
  <home>/work/<video id>/              downloaded audio, kept until `ktr clean`
  <home>/work/_queue/                  the shared queue (jobs, status, lock, log)

<home> is $K_TRANSCRIPTOR_HOME, else %LOCALAPPDATA%\k_transcriptor on Windows, else ~/.local/share/k_transcriptor.
"""
import os
from pathlib import Path

ENV_HOME = "K_TRANSCRIPTOR_HOME"


def home() -> Path:
    custom = os.environ.get(ENV_HOME)
    if custom:
        return Path(custom).expanduser()
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "k_transcriptor"
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / "k_transcriptor"


def transcripts_dir() -> Path:
    return home() / "transcripts"


def work_dir() -> Path:
    return home() / "work"
