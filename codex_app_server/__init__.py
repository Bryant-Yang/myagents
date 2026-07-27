"""Codex app-server transport.

This package owns Codex's native long-lived protocol.  It deliberately stays
separate from ``acp``: both satisfy the project's AgentAdapter contract, but
their wire protocols and approval shapes are different.
"""

from .adapter import CodexAppServerAdapter
from .client import CodexAppServerClient, CodexAppServerError

__all__ = [
    "CodexAppServerAdapter",
    "CodexAppServerClient",
    "CodexAppServerError",
]
