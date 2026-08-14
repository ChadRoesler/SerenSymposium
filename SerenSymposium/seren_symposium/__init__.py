"""
SerenSymposium - the front door to the Seren constellation.

Chat in one tab; every sibling service's own /viewer in the others. Memory,
Tools and Cluster are NOT reimplemented here - SerenMemory, SerenWorkbench and
SerenLodestar already serve those pages on the shared Meninges baseplate, and
Symposium loads them.

TWO CHANNELS, kept deliberately separate:

  DATA  Symposium -> Lodestar -> observatories -> back. Single endpoint
        contract: this app talks only to the cluster head and does not know
        MCP exists. Lodestar owns routing and service spin-up.

  EYES  The Memory/Tools/Cluster tabs load each service's /viewer directly.
        Read-only human browsing, no orchestration.

If chat traffic ever starts going direct to a leaf service, the single-endpoint
contract is broken and Lodestar stops being the cluster head. Don't.
"""
from __future__ import annotations

try:
    from ._version import version as __version__
except Exception:  # noqa: BLE001 - source checkout with no build
    __version__ = "0.0.0+unknown"

__all__ = ["__version__"]
