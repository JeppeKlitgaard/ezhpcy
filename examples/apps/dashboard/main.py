"""A small NiceGUI dashboard, run by EzHPCy as an app on a host."""

import os
import platform
from pathlib import Path

from nicegui import ui


@ui.page("/")
def index() -> None:
    ui.label(f"Hello from {platform.node()}").classes("text-2xl")
    ui.label(f"Working directory: {Path.cwd()}")
    count = ui.label("0")
    ui.button("Count", on_click=lambda: count.set_text(str(int(count.text) + 1)))


# `uds` is passed on to uvicorn, which listens on the Unix socket EzHPCy chose.
ui.run(uds=os.environ["EZHPCY_SOCKET"], show=False, reload=False)
