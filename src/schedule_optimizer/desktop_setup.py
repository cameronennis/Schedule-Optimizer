"""Connect the Claude Desktop helper by editing Claude Desktop's config file (developer setup).

"Add it for me" adds a "schedule-optimizer" entry under "mcpServers" and keeps
every other entry. Before writing, it saves a copy of the file in the app's data
folder, so "Undo" can put the original back if anything goes wrong. The release
build installs the helper with a .mcpb file instead (Phase 9).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from .paths import data_dir

SERVER_NAME = "schedule-optimizer"
BACKUP_NAME = "claude_desktop_config.backup.json"
NO_FILE_MARKER = "claude_desktop_config.backup.none"  # the file didn't exist before we added it


class ConfigError(Exception):
    pass


def config_path() -> Path:
    """Where Claude Desktop keeps its config on this computer."""
    if override := os.environ.get("SCHEDULE_OPTIMIZER_CLAUDE_CONFIG"):  # tests
        return Path(override)
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"
    if sys.platform == "win32":
        return Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming")) / "Claude" / "claude_desktop_config.json"
    return Path.home() / ".config" / "Claude" / "claude_desktop_config.json"


def server_entry() -> dict:
    """How Claude Desktop should start the helper from this checkout (uv run)."""
    project = Path(__file__).resolve().parents[2]
    uv = shutil.which("uv") or "uv"
    return {"command": uv, "args": ["run", "--directory", str(project), "schedule-optimizer-mcp"]}


def snippet() -> str:
    """The text for manual setup: a whole file, for when the config is empty or missing."""
    return json.dumps({"mcpServers": {SERVER_NAME: server_entry()}}, indent=2)


def entry_snippet() -> str:
    """Just the entry, for adding inside an existing "mcpServers" section."""
    return json.dumps({SERVER_NAME: server_entry()}, indent=2)[1:-1].strip("\n")


def _read(path: Path) -> dict:
    if not path.exists() or not path.read_text(encoding="utf-8").strip():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise ConfigError(f"Claude Desktop's config file isn't valid JSON (line {e.lineno}), so it wasn't "
                          "changed. Fix it or set it up manually.") from e
    if not isinstance(data, dict) or not isinstance(data.get("mcpServers", {}), dict):
        raise ConfigError("Claude Desktop's config file has an unexpected layout, so it wasn't changed. "
                          "Set it up manually.")
    return data


def is_configured() -> bool:
    try:
        return SERVER_NAME in _read(config_path()).get("mcpServers", {})
    except (ConfigError, OSError):
        return False


def _backup_dir() -> Path:
    return data_dir()


def has_backup() -> bool:
    d = _backup_dir()
    return (d / BACKUP_NAME).exists() or (d / NO_FILE_MARKER).exists()


def add_entry() -> Path:
    """Add the helper to Claude Desktop's config, saving a copy first. Returns the config path."""
    path = config_path()
    data = _read(path)  # refuses a file it can't safely edit, before touching anything
    d = _backup_dir()
    d.mkdir(parents=True, exist_ok=True)
    for old in (d / BACKUP_NAME, d / NO_FILE_MARKER):
        old.unlink(missing_ok=True)
    if path.exists():
        shutil.copy2(path, d / BACKUP_NAME)
    else:
        (d / NO_FILE_MARKER).write_text("", encoding="utf-8")
    data.setdefault("mcpServers", {})[SERVER_NAME] = server_entry()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)  # atomic: Claude Desktop never sees a half-written file
    if not is_configured():  # read it back to be sure
        undo()
        raise ConfigError("The change didn't stick, so the original file was restored.")
    return path


def undo() -> None:
    """Put Claude Desktop's config back exactly as it was before add_entry()."""
    path, d = config_path(), _backup_dir()
    if (d / BACKUP_NAME).exists():
        shutil.copy2(d / BACKUP_NAME, path)
        (d / BACKUP_NAME).unlink()
    elif (d / NO_FILE_MARKER).exists():
        path.unlink(missing_ok=True)
        (d / NO_FILE_MARKER).unlink()
    else:
        raise ConfigError("There's no saved copy to restore.")


def open_folder() -> None:
    """Show the config file's folder in Finder / Explorer / the file manager."""
    folder = config_path().parent
    folder.mkdir(parents=True, exist_ok=True)
    if sys.platform == "darwin":
        subprocess.run(["open", str(folder)], check=False)
    elif sys.platform == "win32":
        os.startfile(folder)  # type: ignore[attr-defined]
    else:
        subprocess.run(["xdg-open", str(folder)], check=False)
