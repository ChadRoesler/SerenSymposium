"""
seren_symposium.__main__
════════════════════════════════════════════════════════════════════════

Two ways to run, and the difference is only the window:

    seren-symposium              native window (needs the [desktop] extra)
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


def main() -> None:
    p = argparse.ArgumentParser(description="SerenSymposium - front door to the Seren stack")
    p.add_argument("--config", "-c", default=None,
                   help="Path to seren-symposium.yaml (default: ./seren-symposium.yaml "
                        "or $SEREN_SYMPOSIUM_CONFIG)")
    p.add_argument("--serve", action="store_true",
                   help="Serve only; do not open a native window")
    p.add_argument("--port", type=int, default=0, help="Override the loopback UI port")
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

    import uvicorn
    server = uvicorn.Server(uvicorn.Config(app, host=cfg.ui.host, port=cfg.ui.port,
                                           log_level="warning"))

    if args.serve:
        server.run()
        return

    try:
        import webview
    except ImportError:
        print("[symposium] pywebview not installed - serving headless instead.\n"
              "            pip install 'seren-symposium[desktop]' for a native window,\n"
              f"            or open {url} in a browser.", file=sys.stderr)
        server.run()
        return

    # uvicorn in a thread, webview on the main thread: most GUI toolkits insist
    # on owning the main thread, and pywebview is no exception.
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    # A window onto nothing is worse than no window: it looks like the app is
    # broken rather than like port 7426 is taken (often by another Symposium).
    if not came_up(server, t):
        print(f"[symposium] nothing is serving {url} - is port {cfg.ui.port} taken? "
              f"(another Symposium?) Use --port or ui.port.", file=sys.stderr)
        raise SystemExit(1)

    webview.create_window("Seren", url,
                          width=cfg.ui.window_width, height=cfg.ui.window_height)
    webview.start()


if __name__ == "__main__":
    main()
