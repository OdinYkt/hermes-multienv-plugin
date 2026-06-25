"""Multitool plugin — multi-environment tool access for Hermes Agent.

Registers 4 tools that let the agent work with multiple SSH and Docker
environments simultaneously:

  env_connect     — create a named connection
  env_list        — list active connections
  env_tool        — execute a tool operation on a named environment
  env_disconnect  — close a connection and release resources
"""

import logging

from multienv.schemas import (
    ENV_CONNECT_SCHEMA,
    ENV_DISCONNECT_SCHEMA,
    ENV_LIST_SCHEMA,
    ENV_TOOL_SCHEMA,
)
from multienv.registry import registry as _registry
from multienv.handlers import (
    handle_env_connect,
    handle_env_disconnect,
    handle_env_list,
    handle_env_tool,
)

logger = logging.getLogger(__name__)

_TOOLS = [
    ("env_connect", ENV_CONNECT_SCHEMA, handle_env_connect, "🔗"),
    ("env_list", ENV_LIST_SCHEMA, handle_env_list, "📋"),
    ("env_tool", ENV_TOOL_SCHEMA, handle_env_tool, "🔧"),
    ("env_disconnect", ENV_DISCONNECT_SCHEMA, handle_env_disconnect, "🔌"),
]


def register(ctx) -> None:
    """Plugin entry point — called by Hermes plugin discovery."""
    for name, schema, handler, emoji in _TOOLS:
        ctx.register_tool(
            name=name,
            toolset="multienv",
            schema=schema,
            handler=handler,
            emoji=emoji,
        )

    ctx.register_hook("on_session_end", _on_session_end)
    logger.info("multienv plugin registered %d tools", len(_TOOLS))


def _on_session_end(**kwargs) -> None:
    """Lifecycle hook — cleanup all environments on session end."""
    logger.info("multienv: on_session_end — cleaning up all environments")
    _registry.cleanup_all()
