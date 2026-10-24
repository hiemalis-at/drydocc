"""drydocc agent tools: the streamlined, scriptable core of the skill workflows.

Every module here is a pure or subprocess-level building block that one or
more skills call through the MCP server (``mcp_server.py``) instead of
re-deriving inline. Nothing in this package talks to KeePass, writes under
``nodes/`` outside the bound node, or echoes a secret value.
"""

__all__ = ["audit_probe", "doc", "docc", "lingering", "node_io", "render", "state"]
