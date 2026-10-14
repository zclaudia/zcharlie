"""One registration module per MCP tool domain (D4).

Each module exposes a ``register_*`` function that takes the server and the
shared :class:`~investment_agent.mcp_server.plumbing.ServerContext`;
``server.py`` calls them in the order that fixes the tool catalogue
(``TOOL_NAMES``).

The handlers keep their imports inside the tool bodies - as they were when
``build_server`` declared them inline - so listing the catalogue does not
import the domain, storage or API layers.
"""
