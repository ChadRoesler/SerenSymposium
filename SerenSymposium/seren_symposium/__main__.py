"""
seren_symposium.__main__
════════════════════════════════════════════════════════════════════════

The ways to run, and the difference is only who holds the window:

    seren-symposium              a native window (the [desktop] extra) - onto
                                 the Symposium already serving if there is
                                 one, else serving for as long as it is open
    seren-symposium --tray       serve, with an icon in the tray and no window
                                 until you open one: what starts at logon
    seren-symposium --window     only a window, onto the Symposium serving
                                 (what the tray's Open runs)
    seren-symposium --serve      no window; open the URL in a browser

--serve is not a fallback, it is the headless path: it is what CI exercises,
what a Jetson with no display can run, and what you want when debugging the UI
with browser devtools. The [desktop] extra is genuinely optional.

Started with no console (pythonw, a Start menu shortcut) there is no stdout
or stderr at all, and uvicorn's log setup dies asking None whether it is a
terminal. So output goes to symposium.log beside the config instead - which
is also the first place to look when the window never opens.
"""
from __future__ import annotations

import argparse
import atexit
import logging
import os
import sys
import threading
import time
from pathlib import Path

START_SECONDS = 30.0
LOG_NAME = "symposium.log"
LOG_MAX_BYTES = 1_000_000


def log_without_console(config: str | None) -> Path | None:
    """With no console, point stdout and stderr at symposium.log beside the
    config (or in ~/seren-symposium without one) and say where. With a
    console, leave them alone and answer None. A log past LOG_MAX_BYTES
    starts over rather than growing forever."""
    if sys.stdout is not None and sys.stderr is not None:
        return None
    cfg = Path(os.path.expanduser(config or os.environ.get("SEREN_SYMPOSIUM_CONFIG") or "seren-symposium.yaml"))
    folder = cfg.resolve().parent if cfg.is_file() else Path.home() / "seren-symposium"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / LOG_NAME
    mode = "w" if path.is_file() and path.stat().st_size > LOG_MAX_BYTES else "a"
    out = open(path, mode, encoding="utf-8", buffering=1)              # noqa: SIM115 - lives as long as the process
    sys.stdout = sys.stdout or out
    sys.stderr = sys.stderr or out
    return path


ICON = Path(__file__).with_name("symposium.ico")      # the favicon, as the window's icon


def own_taskbar_button() -> None:
    """On Windows, a pythonw window is grouped under Python's taskbar button
    and wears Python's icon. An app id of its own gives Symposium its own
    button, with the window's icon on it."""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("Seren.Symposium")
    except (AttributeError, OSError):
        pass


def webview_profile(cfg) -> Path:
    """The window's browser profile: a `webview` folder beside the sessions
    folder, made if missing."""
    path = cfg.sessions_dir().parent / "webview"
    path.mkdir(parents=True, exist_ok=True)
    return path


def came_up(server, thread, seconds: float = START_SECONDS, step: float = 0.05) -> bool:
    """Wait for uvicorn to be serving. False as soon as its thread has died
    (a port already taken: uvicorn logs it and exits the thread), or when it
    has not come up in `seconds`."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if getattr(server, "started", False):
            return True
        if not thread.is_alive():
            return False
        time.sleep(step)
    return bool(getattr(server, "started", False))


def pid_file_path(cfg, override: str | None = None) -> Path:
    """Where the serving Symposium leaves its pid: beside the config, which
    is what the Observatory's manifest for it names (the Starwright card
    writes both) - or where --pid-file says."""
    if override:
        return Path(override)
    if cfg.config_path:
        return Path(cfg.config_path).with_name("seren-symposium.pid")
    return cfg.sessions_dir().parent / "seren-symposium.pid"


def write_pid(path: Path) -> Path:
    """Our pid into `path`, taken away again at exit (only if it is still
    ours: a newer Symposium may have written its own by then). Symposium is a
    per-user app, not a service, so this file is how its node's Observatory
    knows it is running, stops it, and starts it again after an upgrade
    (the `launcher` service type in seren_observatory.lifecycle)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(os.getpid()), encoding="utf-8")

    def gone() -> None:
        try:
            if path.read_text(encoding="utf-8").strip() == str(os.getpid()):
                path.unlink()
        except OSError:
            pass
    atexit.register(gone)
    return path


def already_serving(url: str, seconds: float = 1.5) -> bool:
    """Is a Symposium answering at `url` - not just anything on the port?"""
    import json
    import urllib.request
    try:
        with urllib.request.urlopen(url.rstrip("/") + "/health", timeout=seconds) as r:
            return json.loads(r.read().decode("utf-8")).get("service") == "SerenSymposium"
    except Exception:  # noqa: BLE001 - refused, timed out, not JSON: not us
        return False


def open_window(cfg, url: str) -> None:
    """A window onto `url`, until it is closed. No pywebview: the browser."""
    try:
        import webview
    except ImportError:
        import webbrowser
        print("[symposium] pywebview not installed - opening your browser instead.", file=sys.stderr)
        webbrowser.open(url)
        return
    own_taskbar_button()
    # text_select: pywebview turns selection off unless asked, and a chat you
    # cannot copy a line out of is not much of a chat.
    webview.create_window("Seren Symposium", url, text_select=True,
                          width=cfg.ui.window_width, height=cfg.ui.window_height)
    # pywebview starts private by default, which empties localStorage every
    # time the window closes: a project with no chat yet, the folded projects,
    # the tab and the screen-share choices were all gone on the next launch.
    # The profile lives beside the sessions, so it moves with them.
    webview.start(private_mode=False, storage_path=str(webview_profile(cfg)), icon=str(ICON))


def main() -> None:
    p = argparse.ArgumentParser(description="SerenSymposium - front door to the Seren stack")
    p.add_argument("--config", "-c", default=None,
                   help="Path to seren-symposium.yaml (default: ./seren-symposium.yaml "
                        "or $SEREN_SYMPOSIUM_CONFIG)")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--serve", action="store_true",
                      help="Serve only; do not open a native window")
    mode.add_argument("--tray", action="store_true",
                      help="Serve with an icon in the tray; a window only when you open one (what starts at logon)")
    mode.add_argument("--window", action="store_true",
                      help="Only a window, onto the Symposium already serving")
    p.add_argument("--port", type=int, default=0, help="Override the loopback UI port")
    p.add_argument("--pid-file", default=None,
                   help="Where the serving Symposium writes its pid (default: seren-symposium.pid beside the config)")
    args = p.parse_args()
    log_without_console(args.config)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s",
                        datefmt="%Y-%m-%d %H:%M:%S", stream=sys.stderr)

    from .app import APP_VERSION, create_app
    from .config import load_config

    cfg = load_config(args.config)
    if args.port:
        cfg.ui.port = args.port

    url = f"http://{cfg.ui.host}:{cfg.ui.port}"
    here = f"http://{'127.0.0.1' if cfg.ui.host in ('0.0.0.0', '::', '') else cfg.ui.host}:{cfg.ui.port}"
    serving = not args.serve and already_serving(here)
    if args.window or (serving and not args.tray):
        # The tray (or another window) is serving already: be a window onto it.
        if not serving:
            print(f"[symposium] nothing is serving {url} - start it (seren-symposium --tray) first.",
                  file=sys.stderr)
            raise SystemExit(1)
        open_window(cfg, url)
        return
    if args.tray and serving:
        print(f"[symposium] already running at {url} - one tray is plenty.", file=sys.stderr)
        return
    print(f"[symposium] v{APP_VERSION}", file=sys.stderr)
    print(f"[symposium] lodestar: {cfg.lodestar.url}", file=sys.stderr)
    print(f"[symposium] ui:       {url}", file=sys.stderr)
    # Not a warning any more. The shim holds the Lodestar bearer and checks
    # nobody's, so a bind past loopback is refused unless the operator said
    # allow_open_lan - and then it says so every boot.
    from seren_meninges.exposure import enforce_exposure
    enforce_exposure(cfg.ui.host, cfg.ui.port, service="seren-symposium",
                     allow_open_lan=cfg.ui.allow_open_lan, env_prefix="SEREN_SYMPOSIUM",
                     supports_token=False, log=lambda m: print(m, file=sys.stderr))

    app = create_app(cfg)
    # Past the window-only paths: whatever follows serves, and the pid file
    # is the Observatory's handle on it.
    print(f"[symposium] pid file: {write_pid(pid_file_path(cfg, args.pid_file))}", file=sys.stderr)

    import uvicorn
    server = uvicorn.Server(uvicorn.Config(app, host=cfg.ui.host, port=cfg.ui.port,
                                           log_level="warning"))

    if args.serve:
        server.run()
        return

    from . import tray
    if args.tray and not tray.available():
        print("[symposium] no tray here (pystray and Pillow: the [desktop] extra) - serving headless.",
              file=sys.stderr)
        server.run()
        return
    if not args.tray:
        try:
            import webview  # noqa: F401
        except ImportError:
            print("[symposium] pywebview not installed - serving headless instead.\n"
                  "            pip install 'seren-symposium[desktop]' for a native window,\n"
                  f"            or open {url} in a browser.", file=sys.stderr)
            server.run()
            return

    # uvicorn in a thread; the window or the tray on the main thread - GUI
    # toolkits insist on owning it.
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    # A window onto nothing is worse than no window: it looks like the app is
    # broken rather than like port 7426 is taken (often by another Symposium).
    if not came_up(server, t):
        print(f"[symposium] nothing is serving {url} - is port {cfg.ui.port} taken? "
              f"(another Symposium?) Use --port or ui.port.", file=sys.stderr)
        raise SystemExit(1)

    if args.tray:
        own_taskbar_button()
        tray.run(url, cfg.config_path or args.config, stop=lambda: setattr(server, "should_exit", True))
        t.join(timeout=10)
        return
    open_window(cfg, url)


if __name__ == "__main__":
    main()
