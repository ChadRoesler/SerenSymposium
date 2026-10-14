"""
seren_symposium.screen
════════════════════════════════════════════════════════════════════════

Screen share, the old SerenChat way: a still of this desktop - or of one
window on it - taken here and handed to the chat like any other image.

    polling  the shell asks for a fresh frame every few seconds; the newest
             one rides along with your next message
    push     a button takes one, you look at it, and send it or don't

A frame is kept beside the chat but marked `screen`, so the shell shows it in
the screen-share panel rather than in the conversation or the Files list.

WHY STILLS AND NOT VIDEO:
    Whoever answers sees a turn, not a feed - Claude reads the frame that came
    with the message, and nothing between messages. A frame every few seconds
    is everything an answer can use, at a fraction of the bytes.

WHY HERE AND NOT IN THE WEBVIEW:
    getDisplayMedia asks every time and is patchy inside a webview; mss on the
    Python side is one call on Windows, macOS and X11. It is the [desktop]
    (or [screen]) extra: without mss and Pillow the panel says so, nothing breaks.

WINDOWS (the kind with title bars) are Windows-only: PrintWindow asks the
window to draw itself, so one half-covered by another still comes out whole.
Elsewhere the list is empty and displays are the only source.
"""
from __future__ import annotations

import io
import sys
import time
from typing import Any, Optional


def available() -> bool:
    try:
        import mss  # noqa: F401
        import PIL  # noqa: F401
    except ImportError:
        return False
    return True


def monitors() -> list[dict[str, Any]]:
    """The screens mss sees, 1-based; [] when it cannot look. `primary` is the
    one at the desktop's origin - the main display, and the default."""
    if not available():
        return []
    import mss
    try:
        with mss.mss() as grab:
            mons = [{"index": i, "width": m["width"], "height": m["height"],
                     "primary": m["left"] == 0 and m["top"] == 0}
                    for i, m in enumerate(grab.monitors) if i > 0]
    except Exception:  # noqa: BLE001 - no display (a headless box) is not an error
        return []
    if mons and not any(m["primary"] for m in mons):
        mons[0]["primary"] = True
    return mons


def primary(mons: list[dict[str, Any]]) -> int:
    return next((m["index"] for m in mons if m.get("primary")), 1)


# ── Windows (the OS) windows ────────────────────────────────────────────────

def _win32():
    """user32/gdi32/dwmapi with the signatures this module uses. Handles are
    pointer-sized: without argtypes ctypes would pass them as 32-bit ints."""
    import ctypes
    from ctypes import wintypes as w
    u, g, d = ctypes.windll.user32, ctypes.windll.gdi32, ctypes.windll.dwmapi
    H = ctypes.c_void_p
    sig = {
        (u, "IsWindow"): ([w.HWND], w.BOOL),
        (u, "IsWindowVisible"): ([w.HWND], w.BOOL),
        (u, "IsIconic"): ([w.HWND], w.BOOL),
        (u, "GetWindow"): ([w.HWND, ctypes.c_uint], w.HWND),
        (u, "GetWindowLongW"): ([w.HWND, ctypes.c_int], ctypes.c_long),
        (u, "GetWindowTextLengthW"): ([w.HWND], ctypes.c_int),
        (u, "GetWindowTextW"): ([w.HWND, w.LPWSTR, ctypes.c_int], ctypes.c_int),
        (u, "GetWindowRect"): ([w.HWND, ctypes.POINTER(w.RECT)], w.BOOL),
        (u, "GetDC"): ([w.HWND], H),
        (u, "ReleaseDC"): ([w.HWND, H], ctypes.c_int),
        (u, "PrintWindow"): ([w.HWND, H, ctypes.c_uint], w.BOOL),
        (g, "CreateCompatibleDC"): ([H], H),
        (g, "CreateCompatibleBitmap"): ([H, ctypes.c_int, ctypes.c_int], H),
        (g, "SelectObject"): ([H, H], H),
        (g, "DeleteObject"): ([H], w.BOOL),
        (g, "DeleteDC"): ([H], w.BOOL),
        (g, "GetDIBits"): ([H, H, ctypes.c_uint, ctypes.c_uint, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint],
                           ctypes.c_int),
        (d, "DwmGetWindowAttribute"): ([w.HWND, w.DWORD, ctypes.c_void_p, w.DWORD], ctypes.c_long),
    }
    for (lib, name), (args, res) in sig.items():
        fn = getattr(lib, name)
        fn.argtypes, fn.restype = args, res
    try:
        u.SetThreadDpiAwarenessContext.argtypes = [H]
        u.SetThreadDpiAwarenessContext.restype = H
    except AttributeError:      # before Windows 10 1607
        pass
    return ctypes, w, u, g, d


def _title(u, ctypes, hwnd) -> str:
    n = u.GetWindowTextLengthW(hwnd)
    if n <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(n + 1)
    u.GetWindowTextW(hwnd, buf, n + 1)
    return buf.value


def windows() -> list[dict[str, Any]]:
    """The windows a person would call windows: visible, titled, not minimised,
    not a tool palette, not owned by another, not a cloaked UWP ghost. [] off
    Windows. `id` is the window handle; it means nothing after the window closes."""
    if sys.platform != "win32" or not available():
        return []
    ctypes, w, u, _, d = _win32()
    out: list[dict[str, Any]] = []
    GW_OWNER, GWL_EXSTYLE, WS_EX_TOOLWINDOW, DWMWA_CLOAKED = 4, -20, 0x80, 14

    @ctypes.WINFUNCTYPE(w.BOOL, w.HWND, w.LPARAM)
    def each(hwnd, _):
        if not hwnd or not u.IsWindowVisible(hwnd) or u.IsIconic(hwnd) or u.GetWindow(hwnd, GW_OWNER):
            return True
        if u.GetWindowLongW(hwnd, GWL_EXSTYLE) & WS_EX_TOOLWINDOW:
            return True
        cloaked = w.DWORD()
        if d.DwmGetWindowAttribute(hwnd, DWMWA_CLOAKED, ctypes.byref(cloaked), ctypes.sizeof(cloaked)) == 0 \
                and cloaked.value:
            return True
        title = _title(u, ctypes, hwnd)
        if title and title != "Program Manager":
            out.append({"id": int(hwnd), "title": title[:200]})
        return True

    ctypes.windll.user32.EnumWindows(each, 0)
    return out


def _grab_window(hwnd: int):
    """One window, drawn by itself, as a PIL image; None when it would not."""
    from PIL import Image
    ctypes, w, u, g, d = _win32()
    if not u.IsWindow(hwnd) or u.IsIconic(hwnd):
        return None
    # Real pixels, not the 96-dpi fiction a DPI-unaware thread is told.
    old = None
    if hasattr(u, "SetThreadDpiAwarenessContext"):
        old = u.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))   # PER_MONITOR_AWARE_V2
    try:
        rect = w.RECT()
        u.GetWindowRect(hwnd, ctypes.byref(rect))
        # The visible frame, without the invisible resize border around it.
        vis = w.RECT()
        if d.DwmGetWindowAttribute(hwnd, 9, ctypes.byref(vis), ctypes.sizeof(vis)) != 0:   # EXTENDED_FRAME_BOUNDS
            vis = rect
        width, height = rect.right - rect.left, rect.bottom - rect.top
        if width <= 0 or height <= 0:
            return None
        screen_dc = u.GetDC(None)
        dc = g.CreateCompatibleDC(screen_dc)
        bmp = g.CreateCompatibleBitmap(screen_dc, width, height)
        prev = g.SelectObject(dc, bmp)
        try:
            if not u.PrintWindow(hwnd, dc, 2):                          # PW_RENDERFULLCONTENT
                return None

            class BITMAPINFOHEADER(ctypes.Structure):
                _fields_ = [("biSize", w.DWORD), ("biWidth", w.LONG), ("biHeight", w.LONG),
                            ("biPlanes", w.WORD), ("biBitCount", w.WORD), ("biCompression", w.DWORD),
                            ("biSizeImage", w.DWORD), ("biXPelsPerMeter", w.LONG),
                            ("biYPelsPerMeter", w.LONG), ("biClrUsed", w.DWORD), ("biClrImportant", w.DWORD)]
            bmi = BITMAPINFOHEADER(biSize=ctypes.sizeof(BITMAPINFOHEADER), biWidth=width,
                                   biHeight=-height, biPlanes=1, biBitCount=32)
            buf = ctypes.create_string_buffer(width * height * 4)
            if not g.GetDIBits(dc, bmp, 0, height, buf, ctypes.byref(bmi), 0):
                return None
        finally:
            g.SelectObject(dc, prev)
            g.DeleteObject(bmp)
            g.DeleteDC(dc)
            u.ReleaseDC(None, screen_dc)
    finally:
        if old:
            u.SetThreadDpiAwarenessContext(old)
    img = Image.frombuffer("RGB", (width, height), buf, "raw", "BGRX", 0, 1)
    box = (max(0, vis.left - rect.left), max(0, vis.top - rect.top),
           min(width, vis.right - rect.left), min(height, vis.bottom - rect.top))
    return img.crop(box) if box[2] > box[0] and box[3] > box[1] else img


# ── A frame ─────────────────────────────────────────────────────────────────

class Frame:
    def __init__(self, jpeg: bytes, width: int, height: int, monitor: int, window: str = ""):
        self.jpeg, self.width, self.height, self.monitor, self.window = jpeg, width, height, monitor, window
        self.at = round(time.time(), 3)

    def info(self) -> dict[str, Any]:
        return {"at": self.at, "width": self.width, "height": self.height,
                "monitor": self.monitor, "window": self.window, "bytes": len(self.jpeg)}


def _jpeg(img, max_width: int, quality: int) -> tuple[bytes, int, int]:
    from PIL import Image
    if max_width and img.width > max_width:
        img = img.resize((max_width, round(img.height * max_width / img.width)), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality, optimize=True)
    return buf.getvalue(), img.width, img.height


def capture(monitor: int = 1, max_width: int = 1600, quality: int = 80,
            window: Optional[int] = None) -> Frame:
    """One still of `monitor` (0 = every screen at once) - or, given `window`,
    of that one window - as a JPEG no wider than max_width. Raises
    RuntimeError with a sentence when it cannot."""
    if not available():
        raise RuntimeError("screen share needs mss and Pillow: pip install 'seren-symposium[screen]'")
    if window is not None:
        found = next((x for x in windows() if x["id"] == window), None)
        if found is None:
            raise RuntimeError("that window is gone (closed or minimised) - pick it again")
        img = _grab_window(window)
        if img is None:
            raise RuntimeError(f"{found['title']} would not draw itself - try the whole display")
        jpeg, wd, ht = _jpeg(img, max_width, quality)
        return Frame(jpeg, wd, ht, 0, found["title"])
    import mss
    from PIL import Image
    with mss.mss() as grab:
        if not 0 <= monitor < len(grab.monitors):
            raise RuntimeError(f"there is no monitor {monitor} (this desktop has {len(grab.monitors) - 1})")
        shot = grab.grab(grab.monitors[monitor])
    img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
    jpeg, wd, ht = _jpeg(img, max_width, quality)
    return Frame(jpeg, wd, ht, monitor)
