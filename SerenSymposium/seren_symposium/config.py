"""
seren_symposium.config
════════════════════════════════════════════════════════════════════════

Config for a CLIENT, not a service. Symposium never authenticates anyone; it
holds the pointer to a bearer it PRESENTS to Lodestar. Same resolution chain as
every other service (inline / keyring / env) via seren_meninges.resolve_token,
so a token lives in exactly one place per machine.

FOUR LAYERS, split by how often you touch a thing rather than what it is:

    per-install    lodestar url, ui port, confirm_destructive   (this yaml)
    per-folder     voice, system prompt, model, temperature, ... (folder preset)
    per-session    overrides of the above                       (session settings)
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
from typing import Any, Optional, Union

import yaml
from pydantic import BaseModel, Field

from seren_meninges import TlsConfig, resolve_token

log = logging.getLogger(__name__)

# Loopback UI port. Family map: lodestar 6361 · memory 7420 · margin 7421 ·
# loci 7422 · corpus-callosum 7423 · hippocampus 7424 · workbench 7425 · symposium 7426 ·
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
    # Who answers: "lodestar" (a local model, routed by Lodestar) or "claude"
    # (the claude CLI on this machine - see ClaudeConfig). "" = lodestar.
    voice: str = ""
    system_prompt: str = ""
    model: str = ""
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None
    repeat_penalty: Optional[float] = None
    # Which memory tags sessions here should read. Empty = no filter.
    # NOT WIRED YET: nothing reads it.
    memory_tags: list[str] = Field(default_factory=list)


class LodestarClientConfig(BaseModel):
    """WHERE TO CONNECT, plus the token pointer. Note this is the client half
    of the family's ServerConfig - there is no host/port to bind, because
    Symposium is not the thing being connected TO."""
    url: str = DEFAULT_LODESTAR

    # LONGER than Lodestar's own 120s llama timeout, on purpose. When a model is
    # cold-loading into VRAM the upstream call can run long, and Lodestar turns
    # that into a 504 that names the node and says "retry". If our timeout fired
    # first we would replace that useful answer with a generic 502 "unreachable"
    # at the exact moment the head was about to explain itself. The client must
    # outlive the server it is quoting.
    timeout_seconds: float = 180.0

    # How much conversation history we put on the wire, in characters.
    #
    # Lodestar caps context at SEREN_CTX_BUDGET (6000 tokens, ~18000 chars) but
    # its trimmer ONLY rewrites tool-response messages - ordinary back-and-forth
    # is never trimmed, so an unbounded history silently sails past the budget
    # and lands on llama.cpp oversized. Bounding it is therefore the client's
    # job, and ~4000 tokens leaves room for the system prompt, the injected MCP
    # tools block, the new message and the answer.
    #
    # Only what is SENT is trimmed; the shell still shows the whole thread.
    history_max_chars: int = 12000

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


class ClaudeConfig(BaseModel):
    """The claude voice: chats where Claude answers, through the `claude` CLI
    on this machine (seren_symposium.claude_voice has the why).

    Everything that decides WHAT runs lives here, never in a request: the
    program, where it runs, which tools it may use without asking.
    """
    enabled: bool = True
    # What the answers are signed as in the shell ("claude", or a name).
    name: str = "claude"
    # The program and any fixed leading arguments; Symposium adds -p, the
    # stream flags, the session and the per-chat model / system prompt.
    command: list[str] = Field(default_factory=lambda: ["claude"])
    # The working directory: decides which project settings, CLAUDE.md and
    # project MCP servers the CLI loads. Blank = Symposium's own.
    cwd: str = ""
    # Model when neither the folder nor the chat names one. Blank = the CLI's.
    model: str = ""
    # --permission-mode and --allowedTools. With -p nobody is there to answer
    # a permission prompt, so a tool that would ask is refused instead; list
    # the ones this voice may use (e.g. "mcp__workbench__*").
    permission_mode: str = ""
    allowed_tools: list[str] = Field(default_factory=list)
    # Folders Claude may read and work in besides its cwd (--add-dir).
    add_dirs: list[str] = Field(default_factory=list)
    # A tool that is not on the list is asked about IN THE WINDOW - Allow
    # once, Always, Deny - instead of refused (seren_symposium.permit).
    ask: bool = True
    # The Claude Code carabiner clipped on this box: its claude.yaml, or the
    # folder it is in. Blank: KBH_CONFIG, then <cwd>/kbh, then ~/.seren/kbh.
    # Read for the Settings tab only - Symposium never uses its tokens.
    carabiner: str = ""
    # A turn that runs longer is killed; tools and thinking take a while.
    timeout_seconds: float = 900.0
    # How much of the chat goes over when a new CLI conversation picks it up
    # (its first turn, after a rewind or edit, in a fork). Claude reads far
    # more than a local model, so this is not lodestar.history_max_chars.
    handover_max_chars: int = 100000
    # Claude may save files into the chat's own folder - a drawing, notes,
    # code - which then show in the chat. Only there: everything else it may
    # write is still whatever permission_mode and allowed_tools say.
    save_files: bool = True
    # Said to every Claude turn after the chat's own system prompt: where it
    # is answering, and how files and images come and go. {files} is this
    # chat's folder. Blank says nothing.
    framing: str = (
        "You are answering in Seren Symposium, a chat window; your reply is shown as Markdown. "
        "This chat's files are in {files} - when a message attaches one, its path is given, and "
        "you can open it. To show an image, write it as Markdown: ![caption](https://...) for "
        "one on the web (Symposium saves it into the chat), or save a file into that folder - "
        "an SVG you draw, notes, code - and write ![caption](files/<filename>) for a picture or "
        "[name](files/<filename>) for anything else.")


class ServiceViewers(BaseModel):
    """Where the read-only tabs point.

    These are the OTHER services' own /viewer pages - not reimplementations.
    Blank a URL to hide that tab (someone who does not run Probe should not
    have a dead Probe tab).

    A tab can also be a GROUP: a map of names to URLs, shown as one tab with
    its own row of sub-tabs. Memory is one by default, because memory is not
    one service - SerenMemory holds the episodes, Loci the facts, the Callosum
    searches across both and the Hippocampus consolidates them. A plain URL
    there still works, as one tab.
    """
    memory: Union[str, dict[str, str]] = Field(default_factory=lambda: {
        "memory":      "http://127.0.0.1:7420/viewer",
        "loci":        "http://127.0.0.1:7422/viewer",
        "callosum":    "http://127.0.0.1:7423/viewer",
        "hippocampus": "http://127.0.0.1:7424/viewer",
    })
    tools: Union[str, dict[str, str]] = "http://127.0.0.1:7425/viewer"
    cluster: Union[str, dict[str, str]] = "http://127.0.0.1:6361/viewer"
    probe: Union[str, dict[str, str]] = ""

    def shown(self) -> dict[str, Union[str, dict[str, str]]]:
        """The tabs worth drawing: blank URLs dropped, and a group left with
        nothing in it dropped with them."""
        out: dict[str, Union[str, dict[str, str]]] = {}
        for name, v in self.model_dump().items():
            if isinstance(v, dict):
                v = {k: u for k, u in v.items() if u}
            if v:
                out[name] = v
        return out


class UiConfig(BaseModel):
    host: str = "127.0.0.1"     # loopback ONLY - see the note in pyproject
    # The shim has NO inbound auth and holds the Lodestar bearer. Widening it
    # past loopback refuses to start unless this is set (or
    # SEREN_SYMPOSIUM_ALLOW_OPEN_LAN=1) - see seren_meninges.exposure.
    allow_open_lan: bool = False
    port: int = DEFAULT_UI_PORT
    confirm_destructive: bool = True
    # Your name on your own messages.
    you: str = "you"
    window_width: int = 1280
    window_height: int = 860


class UpdatesConfig(BaseModel):
    """"Is there a newer seren-symposium" checking. Cosmetic, opt-outable."""
    enabled: bool = True
    check_interval_hours: float = 6.0
    index_url: str = "https://pypi.org/pypi/{distribution}/json"
    allow_prerelease: bool = False


class SessionsConfig(BaseModel):
    """Where sessions are kept: one JSON file each.

    On disk, in Python, rather than in the webview's localStorage: that is
    per ORIGIN, port included, so moving ui.port (or --port) would have
    silently lost every conversation; pywebview's default private mode keeps
    nothing at all; and a file is something you can back up and read.

    Blank: a `sessions` folder next to seren-symposium.yaml, or
    ~/seren-symposium/sessions when there is no yaml.
    """
    dir: str = ""


class FilesConfig(BaseModel):
    """What a chat can carry. Files live beside the session (sessions.py)."""
    # One attachment's ceiling. Generous on purpose: a local model's limits
    # are the model's to have, not the window's.
    max_mb: float = 25.0
    # Text files go INTO the prompt for a local model (it cannot open a path);
    # this much, across a message's files, and the rest is cut with a note.
    inline_max_chars: int = 20000
    # An answer that shows an image from the web - ![caption](https://...) -
    # gets it saved into the chat, so it is still there next year. This is the
    # one outbound call that is not to Lodestar; off, such an image stays a link.
    fetch_images: bool = True


class ScreenConfig(BaseModel):
    """Screen share: a picture of this desktop, taken here (mss), riding along
    with a message - every few seconds (polling) or on a button (push)."""
    enabled: bool = True
    # Wider frames are scaled down to this; a 4K screenshot is mostly bytes.
    max_width: int = 1600
    jpeg_quality: int = 80


class SymposiumConfig(BaseModel):
    lodestar: LodestarClientConfig = Field(default_factory=LodestarClientConfig)
    files: FilesConfig = Field(default_factory=FilesConfig)
    screen: ScreenConfig = Field(default_factory=ScreenConfig)
    viewers: ServiceViewers = Field(default_factory=ServiceViewers)
    ui: UiConfig = Field(default_factory=UiConfig)
    updates: UpdatesConfig = Field(default_factory=UpdatesConfig)
    tls: TlsConfig = Field(default_factory=TlsConfig)
    sessions: SessionsConfig = Field(default_factory=SessionsConfig)
    claude: ClaudeConfig = Field(default_factory=ClaudeConfig)
    folders: list[FolderPreset] = Field(default_factory=list)
    # The yaml this came from, when there was one. Not a setting.
    config_path: Optional[str] = Field(default=None, exclude=True)

    def folder(self, name: str) -> Optional[FolderPreset]:
        return next((f for f in self.folders if f.name == name), None)

    def sessions_dir(self) -> Path:
        if self.sessions.dir:
            return Path(os.path.expanduser(self.sessions.dir))
        if self.config_path:
            return Path(self.config_path).resolve().parent / "sessions"
        return Path.home() / "seren-symposium" / "sessions"


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
    if v := env.get("SEREN_SYMPOSIUM_SESSIONS_DIR"):
        cfg.sessions.dir = v
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

    data.pop("config_path", None)
    cfg = SymposiumConfig(**data)
    if cfg_path.is_file():
        cfg.config_path = str(cfg_path)
    return _apply_env_overrides(cfg)
