"""Launch the app window:  uv run schedule-optimizer-app   (add --browser to use a browser tab)."""

from __future__ import annotations

import multiprocessing
import sys

from nicegui import ui

from .ui_app import root


def main() -> None:
    multiprocessing.freeze_support()  # required for the PyInstaller build (Phase 9)
    browser = "--browser" in sys.argv
    ui.run(
        root,
        host="127.0.0.1",  # only this computer can connect, in both modes
        title="Schedule Optimizer",
        native=not browser,
        window_size=None if browser else (1100, 820),
        reload=False,
        show=browser,
        port=None if not browser else 8765,
        show_welcome_message=browser,
    )


if __name__ in {"__main__", "__mp_main__"}:
    main()
