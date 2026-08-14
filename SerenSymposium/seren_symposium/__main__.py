"""
seren_symposium.__main__
════════════════════════════════════════════════════════════════════════

Two ways to run, and the difference is only the window:

    seren-symposium              native window (needs the [desktop] extra)
    seren-symposium --serve      no window; open the URL in a browser

--serve is not a fallback, it is the headless path: it is what CI exercises,
what a Jetson with no display can run, and what you want when debugging the UI
with browser devtools. The [desktop] extra is genuinely optional.
"""
from __future__ import annotations

import argparse
import logging
import sys
import threading
import time


def main() -> None:
    p = argparse.ArgumentParser(description="SerenSymposium - front door to the Seren stack")
    p.add_argument("--config", "-c", default=None,
                   help="Path to seren-symposium.yaml (default: ./seren-symposium.yaml "
                        "or $SEREN_SYMPOSIUM_CONFIG)")
    p.add_argument("--serve", action="store_true",
                   help="Serve only; do not open a native window")
    p.add_argument("--port", type=int, default=0, help="Override the loopback UI port")
    args = p.parse_args()

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
    if cfg.ui.host not in ("127.0.0.1", "localhost", "::1"):
        print(f"[symposium] WARNING: ui.host is {cfg.ui.host}, not loopback. This server "
              f"has NO inbound auth by design.", file=sys.stderr)

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
    for _ in range(100):                       # ~5s for the socket to come up
        if getattr(server, "started", False):
            break
        time.sleep(0.05)

    webview.create_window("Seren", url,
                          width=cfg.ui.window_width, height=cfg.ui.window_height)
    webview.start()


if __name__ == "__main__":
    main()
