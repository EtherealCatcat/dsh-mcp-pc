"""Version and identity constants for dsh-pc-remote."""

from __future__ import annotations

__version__ = "1.0.1"
SERVICE_NAME = "dsh-pc-remote"
SERVICE_TITLE = "DSH Remote PC"
MCP_SERVER_NAME = "dshpc"

#: Protocol revisions this server can answer with.  The DSH MCP client
#: negotiates the legacy `initialize` handshake and offers 2025-11-25 first.
SUPPORTED_PROTOCOL_VERSIONS = (
    "2025-11-25",
    "2025-06-18",
    "2025-03-26",
    "2024-11-05",
    "2024-10-07",
)

DEFAULT_PORT = 8765
