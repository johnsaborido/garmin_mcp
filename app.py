"""Vercel entry point: serves the Garmin MCP server over Streamable HTTP.

Vercel's Python preset serves the ``app`` ASGI callable from this file on every
route, so request paths reach the app unchanged. See VERCEL.md for setup.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))

from garmin_mcp.cloud import app  # noqa: E402,F401
