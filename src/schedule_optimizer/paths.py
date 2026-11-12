"""Where the app keeps its files, on macOS and Windows alike.

- User data (settings, later the SQLite database) goes in the OS's per-user app
  folder: ~/Library/Application Support/Schedule Optimizer on macOS,
  %LOCALAPPDATA%\\Schedule Optimizer on Windows.
- The Google OAuth client file is read from Secrets/client_secret.json in the
  project during development, or from $SCHEDULE_OPTIMIZER_CLIENT_SECRET if set.
"""

from __future__ import annotations

import os
from pathlib import Path

from platformdirs import user_data_path

APP_NAME = "Schedule Optimizer"
PROJECT_ROOT = Path(__file__).resolve().parents[2]


def data_dir() -> Path:
    override = os.environ.get("SCHEDULE_OPTIMIZER_DATA_DIR")
    path = Path(override) if override else user_data_path(APP_NAME, appauthor=False)
    path.mkdir(parents=True, exist_ok=True)
    return path


def client_secret_path() -> Path:
    override = os.environ.get("SCHEDULE_OPTIMIZER_CLIENT_SECRET")
    return Path(override) if override else PROJECT_ROOT / "Secrets" / "client_secret.json"
