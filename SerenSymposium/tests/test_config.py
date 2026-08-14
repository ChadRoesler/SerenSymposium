"""Config: the four layers, and the one that is easy to get wrong.

Folders are CONTEXT BOUNDARIES. If a folder's preset stops being inherited,
Symposium silently loses the thing it exists to prevent - context drift - and
nothing errors. So that inheritance gets a test.
"""
from __future__ import annotations

import os

import pytest

from seren_symposium.config import (
    DEFAULT_LODESTAR, DEFAULT_UI_PORT, FolderPreset, SymposiumConfig, load_config,
)


def test_zero_config_is_a_valid_run():
    cfg = SymposiumConfig()
    assert cfg.lodestar.url == DEFAULT_LODESTAR
    assert cfg.ui.port == DEFAULT_UI_PORT
    assert cfg.ui.host == "127.0.0.1", "the UI server has no inbound auth; loopback only"


def test_ui_defaults_to_loopback():
    """Not a style preference. app.py serves an UNAUTHENTICATED API - the
    security property is the bind address, so a default of 0.0.0.0 would be a
    real vulnerability rather than an inconvenience."""
    assert SymposiumConfig().ui.host in ("127.0.0.1", "localhost", "::1")


def test_folder_lookup_returns_the_preset():
    cfg = SymposiumConfig(folders=[
        FolderPreset(name="seren stack", system_prompt="stack context", temperature=0.6),
        FolderPreset(name="general", temperature=0.9),
    ])
    f = cfg.folder("seren stack")
    assert f is not None and f.system_prompt == "stack context" and f.temperature == 0.6
    assert cfg.folder("general").temperature == 0.9
    assert cfg.folder("nope") is None


def test_a_folder_with_no_opinions_is_valid():
    """"No opinion" must be expressible - a folder that only groups, without
    imposing a prompt or temperature, is a legitimate thing to want."""
    f = FolderPreset(name="misc")
    assert f.system_prompt == "" and f.temperature is None and f.memory_tags == []


def test_bearer_is_resolved_never_stored_plain():
    cfg = SymposiumConfig()
    cfg.lodestar.bearer_token_env = "SYMPOSIUM_TEST_TOKEN"
    os.environ["SYMPOSIUM_TEST_TOKEN"] = "sekret"
    try:
        assert cfg.lodestar.resolve_bearer() == "sekret"
    finally:
        del os.environ["SYMPOSIUM_TEST_TOKEN"]


def test_no_token_configured_resolves_empty():
    """An open Lodestar on a trusted LAN is a valid deployment; "" means
    "send no Authorization header", not "fail"."""
    assert SymposiumConfig().lodestar.resolve_bearer() == ""


def test_env_overrides_win_last(monkeypatch, tmp_path):
    cfg_file = tmp_path / "seren-symposium.yaml"
    cfg_file.write_text("# ═══ banner\nlodestar:\n  url: \"http://from-file:6361\"\n",
                        encoding="utf-8")
    cfg = load_config(str(cfg_file))
    assert cfg.lodestar.url == "http://from-file:6361", "yaml should be read"

    monkeypatch.setenv("SEREN_SYMPOSIUM_LODESTAR_URL", "http://from-env:6361")
    assert load_config(str(cfg_file)).lodestar.url == "http://from-env:6361"


def test_banner_in_yaml_does_not_break_the_loader(tmp_path):
    """Every sample config in this family opens with a `# ═══` banner. Read
    without encoding="utf-8" that is a UnicodeDecodeError on Windows, and it
    has bitten every other service at least once."""
    p = tmp_path / "c.yaml"
    p.write_text("# ══════════════════\nui:\n  port: 9999\n", encoding="utf-8")
    assert load_config(str(p)).ui.port == 9999


def test_unreadable_config_degrades_rather_than_crashes(tmp_path):
    p = tmp_path / "bad.yaml"
    p.write_bytes(b"\xff\xfe not: [valid: yaml")
    cfg = load_config(str(p))
    assert cfg.ui.port == DEFAULT_UI_PORT, "a bad config must not stop the app opening"
