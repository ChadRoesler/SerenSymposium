"""
seren_symposium.config
════════════════════════════════════════════════════════════════════════

Config for a CLIENT, not a service. Symposium never authenticates anyone; it
holds the pointer to a bearer it PRESENTS to Lodestar. Same resolution chain as
every other service (inline / keyring / env) via seren_meninges.resolve_token,
so a token lives in exactly one place per machine.

FOUR LAYERS, split by how often you touch a thing rather than what it is:

    per-install    lodestar url, ui port, confirm_destructive   (Config tab)
    per-folder     system prompt, model, temperature            (folder preset)
    per-session    overrides of the above                       (header chip)
    per-message    the composer

The folder layer is the one that is easy to mistake for filing. It isn't:
folders exist to stop CONTEXT DRIFT. A "seren stack" folder opens sessions that
already know what they are about; a "general bullshit" folder opens sessions
that don't inherit any of it. Grouping is the side effect, not the point.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Optional

import yaml
from pydantic import BaseModel, Field

from seren_meninges import TlsConfig, resolve_token

log = logging.getLogger(__name__)

# Loopback UI port. Family map: lodestar 6361 · memory 7420 · margin 7421 ·
# loci 7422 · corpus-callosum 7423 · workbench 7425 · symposium 7426 ·
# probe 7430 · observatory 7777.
DEFAULT_UI_PORT = 7426
DEFAULT_LODESTAR = "http://127.0.0.1:6361"


class FolderPreset(BaseModel):
    """A context boundary with defaults attached.

    Sessions created inside a folder inherit these; the per-session chip can
    still override any of them. Empty string / None means "no opinion, fall
    through to whatever the model or Lodestar defaults to".
    """
    name: str
    system_prompt: str = ""
    model: str = ""
    temperature: Optional[float] = None
    # Which memory tags sessions here should read. Empty = no filter.
    memory_tags: list[str] = Field(default_factory=list)


class LodestarClientConfig(BaseModel):
    """WHERE TO CONNECT, plus the token pointer. Note this is the client half
    of the family's ServerConfig - there is no host/port to bind, because
    Symposium is not the thing being connected TO."""
    url: str = DEFAULT_LODESTAR
    timeout_seconds: float = 120.0

    # Token POINTERS - at most one in practice, same precedence as the family.
    bearer_token: str = ""
    bearer_token_env: str = ""
    bearer_token_keyring: str = ""

    def resolve_bearer(self) -> str:
        """The token Symposium PRESENTS to Lodestar. "" means Lodestar is open."""
        return resolve_token(
            inline=self.bearer_token or None,
            keyring_ref=self.bearer_token_keyring or None,
            env_var=self.bearer_token_env or None,
        )


class ServiceViewers(BaseModel):
    """Where the read-only tabs point.

    These are the OTHER services' own /viewer pages - not reimplementations.
    Blank a URL to hide that tab (someone who does not run Probe should not
    have a dead Probe tab).
    """
    memory: str = "http://127.0.0.1:7420/viewer"
    tools: str = "http://127.0.0.1:7425/viewer"
    cluster: str = "http://127.0.0.1:6361/viewer"
    probe: str = ""


class UiConfig(BaseModel):
    host: str = "127.0.0.1"     # loopback ONLY - see the note in pyproject
    port: int = DEFAULT_UI_PORT
    confirm_destructive: bool = True
    window_width: int = 1280
    window_height: int = 860


class UpdatesConfig(BaseModel):
    """"Is there a newer seren-symposium" checking. Cosmetic, opt-outable."""
    enabled: bool = True
    check_interval_hours: float = 6.0
    index_url: str = "https://pypi.org/pypi/{distribution}/json"
    allow_prerelease: bool = False


class SymposiumConfig(BaseModel):
    lodestar: LodestarClientConfig = Field(default_factory=LodestarClientConfig)
    viewers: ServiceViewers = Field(default_factory=ServiceViewers)
    ui: UiConfig = Field(default_factory=UiConfig)
    updates: UpdatesConfig = Field(default_factory=UpdatesConfig)
    tls: TlsConfig = Field(default_factory=TlsConfig)
    folders: list[FolderPreset] = Field(default_factory=list)

    def folder(self, name: str) -> Optional[FolderPreset]:
        return next((f for f in self.folders if f.name == name), None)


def _apply_env_overrides(cfg: SymposiumConfig) -> SymposiumConfig:
    """SEREN_SYMPOSIUM_* wins last."""
    env = os.environ
    if v := env.get("SEREN_SYMPOSIUM_LODESTAR_URL"):
        cfg.lodestar.url = v
    if v := env.get("SEREN_SYMPOSIUM_BEARER_TOKEN"):
        cfg.lodestar.bearer_token = v
    if v := env.get("SEREN_SYMPOSIUM_BEARER_TOKEN_ENV"):
        cfg.lodestar.bearer_token_env = v
    if v := env.get("SEREN_SYMPOSIUM_BEARER_TOKEN_KEYRING"):
        cfg.lodestar.bearer_token_keyring = v
    if v := env.get("SEREN_SYMPOSIUM_PORT"):
        try:
            cfg.ui.port = int(v)
        except ValueError:
            log.warning("SEREN_SYMPOSIUM_PORT=%r is not an int; keeping %s", v, cfg.ui.port)
    if (v := env.get("SEREN_SYMPOSIUM_UPDATES_ENABLED")) is not None:
        cfg.updates.enabled = v.strip().lower() in ("1", "true", "yes", "on")
    if v := env.get("SEREN_SYMPOSIUM_TRUST_SYSTEM_STORE"):
        cfg.tls.trust_system_store = v.lower() in ("1", "true", "yes", "on")
    return cfg


def load_config(path: Optional[str] = None) -> SymposiumConfig:
    """Defaults -> yaml -> env (later wins). A missing file is a valid run."""
    data: dict[str, Any] = {}
    candidate = path or os.environ.get("SEREN_SYMPOSIUM_CONFIG") or "seren-symposium.yaml"
    cfg_path = Path(os.path.expanduser(candidate))
    if cfg_path.is_file():
        try:
            # encoding= IS NOT OPTIONAL. Without it Python uses the locale
            # codec - cp1252 on Windows - and the sample config opens with a
            # `# ═══` banner, so byte 0x90 raises UnicodeDecodeError. This has
            # bitten every service in the family at least once.
            with open(cfg_path, encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
        except Exception as ex:  # noqa: BLE001
            log.warning("could not read %s: %s - using defaults + env", cfg_path, ex)
            data = {}

    cfg = SymposiumConfig(**data)
    return _apply_env_overrides(cfg)
