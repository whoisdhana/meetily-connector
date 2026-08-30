"""meetily-connector — bridge Meetily meeting data to any agent.

Two modes:
  serve  — stdio MCP server (JSON-RPC 2.0, one object per line)
  watch  — daemon that POSTs meeting.completed payloads to a configured webhook
"""

__version__ = "0.1.0"
