"""MCP integration — Agent → MCP Client → Ops MCP Server → Infrastructure.

The backend speaks the Model Context Protocol directly over stdio (JSON-RPC
2.0, newline delimited). That means the Agent never learns *how* a metric was
fetched; swapping the simulated environment for real Grafana or Kubernetes
only changes what sits behind the MCP server.
"""
