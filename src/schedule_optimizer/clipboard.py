"""Copy text to the system clipboard from Python.

The app window is a web view, and its clipboard API only works during a direct
click, which a round trip to this local server isn't, so `ui.clipboard.write`
silently does nothing in the native window. The app runs on your own computer, so
it can use the operating system's clipboard tool instead.
"""

from __future__ import annotations

import shutil
import subprocess
import sys


def copy(text: str) -> bool:
    """Put `text` on the clipboard. Returns False if no clipboard tool is available."""
    if sys.platform == "darwin":
        cmd, data = ["pbcopy"], text.encode("utf-8")
    elif sys.platform == "win32":
        cmd, data = ["clip"], text.encode("utf-16")  # clip reads UTF-16 with a byte-order mark
    elif shutil.which("wl-copy"):
        cmd, data = ["wl-copy"], text.encode("utf-8")
    elif shutil.which("xclip"):
        cmd, data = ["xclip", "-selection", "clipboard"], text.encode("utf-8")
    else:
        return False
    try:
        subprocess.run(cmd, input=data, check=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return False
    return True
