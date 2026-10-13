"""Vercel entry point: serves the Garmin MCP server over Streamable HTTP.

See VERCEL.md for setup. All routes are rewritten here by vercel.json.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "src"))

from garmin_mcp.cloud import app  # noqa: E402,F401
