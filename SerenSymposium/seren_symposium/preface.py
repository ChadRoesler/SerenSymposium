"""The preface: the page Margin keeps that the room reads to the model as its
system prompt (seren_margin.bookmark). Fetched once per chat turn, cached a
minute, and absent - never an error - when Margin does not answer: the
claude voice then keeps the CLI's own prompt (config.PrefaceConfig).
"""
from __future__ import annotations

import logging
import os
import time
import urllib.error
import urllib.request

from .config import PrefaceConfig

log = logging.getLogger("seren_symposium")

# One page per url; a failure is cached as "" for the same minute so a down
# Margin costs one timeout a minute, not one a turn.
_cache: dict[str, tuple[float, str]] = {}


def text(cfg: PrefaceConfig, now: float | None = None) -> str:
    """The page, stripped, or "" when there is none to be had."""
    if cfg.file:
        try:
            return open(os.path.expanduser(cfg.file), encoding="utf-8").read().strip()
        except OSError as ex:
            log.warning("preface file %s: %s - the CLI keeps its own prompt", cfg.file, ex)
            return ""
    url = (cfg.url or "").strip().rstrip("/")
    if not url:
        return ""
    now = time.monotonic() if now is None else now
    hit = _cache.get(url)
    if hit and now - hit[0] < cfg.cache_seconds:
        return hit[1]
    page = ""
    try:
        req = urllib.request.Request(url + "/preface?format=text")
        if bearer := cfg.resolve_bearer():
            req.add_header("Authorization", f"Bearer {bearer}")
        with urllib.request.urlopen(req, timeout=cfg.timeout_seconds) as r:
            page = r.read().decode("utf-8", "replace").strip()
    except (urllib.error.URLError, OSError, ValueError) as ex:
        log.warning("preface from %s: %s - the CLI keeps its own prompt this minute", url, ex)
    _cache[url] = (now, page)
    return page


def forget() -> None:
    """Drop the cache (tests, and a settings change)."""
    _cache.clear()
