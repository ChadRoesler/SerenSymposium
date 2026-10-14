"""
seren_symposium.tray
════════════════════════════════════════════════════════════════════════

Symposium always on: started at logon (seren-symposium --tray), it serves
and sits in the tray, and the window is something you open onto it - and
close - without stopping it. Like the rest of the stack, it is just there.

WHY AT LOGON, AS YOU, AND NOT A SERVICE:
    A Windows service runs as another account, with no desktop. The claude
    voice needs YOUR claude login and MCP servers, and screen share needs
    your desktop - neither exists for a service. So it starts when you log
    in (the installer puts it in your Startup folder), not at boot.

The window is its own process (seren-symposium --window): pywebview owns
the main thread for as long as a window is open, and the tray must not
wait on it. Quit, in the tray menu, stops the server.
"""
from __future__ import annotations

import subprocess
import sys
import webbrowser
from pathlib import Path
from typing import Callable, Optional

ICON = Path(__file__).with_name("symposium.ico")


def available() -> bool:
    try:
        import pystray  # noqa: F401
        from PIL import Image  # noqa: F401
    except ImportError:
        return False
    return True


def window_command(config: Optional[str]) -> list[str]:
    """How the tray opens a window: this Python, quietly where it can be."""
    exe = Path(sys.executable)
    quiet = exe.with_name("pythonw.exe")
    cmd = [str(quiet if quiet.is_file() else exe), "-m", "seren_symposium", "--window"]
    return cmd + (["--config", config] if config else [])


def run(url: str, config: Optional[str], stop: Callable[[], None], title: str = "Seren Symposium") -> None:
    """The tray icon, on this (the main) thread, until Quit."""
    import pystray
    from PIL import Image

    def open_window(icon=None, item=None):
        subprocess.Popen(window_command(config), close_fds=True,
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))

    def open_browser(icon=None, item=None):
        webbrowser.open(url)

    def quit_(icon, item=None):
        stop()
        icon.stop()

    icon = pystray.Icon("seren-symposium", Image.open(ICON), title, menu=pystray.Menu(
        pystray.MenuItem("Open Symposium", open_window, default=True),
        pystray.MenuItem("Open in your browser", open_browser),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Quit Symposium", quit_)))
    icon.run()
