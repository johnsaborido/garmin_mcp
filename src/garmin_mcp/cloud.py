"""Serverless HTTP entry point for running the Garmin MCP server on Vercel.

Exposes ``app``, a plain ASGI callable that serves the MCP Streamable HTTP
endpoint at ``/mcp/<GARMIN_MCP_SECRET>``. Everything else returns 404, so the
URL itself is the credential (claude.ai custom connectors accept a bare URL).

Environment variables
---------------------
GARMIN_MCP_SECRET      Required. Long random string; part of the MCP URL.
GARMIN_TOKENS_B64      Base64 of the ``garmin_tokens.json`` written by
                       ``garmin-mcp-auth``. Seeds the token store.
KV_REST_API_URL        Optional (set automatically by Vercel's Upstash Redis
KV_REST_API_TOKEN      integration). When present, refreshed Garmin tokens are
                       saved there so they survive cold starts; otherwise the
                       env var above is re-read on every cold start.
GARMIN_READ_ONLY       Default ``true``: only ``get_*`` / ``count_*`` tools are
                       exposed. Set ``false`` to expose write tools too.
GARMIN_ENABLED_TOOLS / GARMIN_DISABLED_TOOLS
                       Same meaning as for the local server; an explicit list
                       overrides GARMIN_READ_ONLY.

Every request runs on a fresh stateless, JSON-response MCP transport, so the
app needs no ASGI lifespan and holds no session state between invocations.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import sys
import tempfile
import threading

import requests

_TOKEN_KEY = "garmin_mcp:tokens"
_TOKEN_DIR = os.path.join(tempfile.gettempdir(), "garmin-tokens")
_TOKEN_FILE = os.path.join(_TOKEN_DIR, "garmin_tokens.json")


# --------------------------------------------------------------------------
# Durable token store (Upstash Redis REST, optional)
# --------------------------------------------------------------------------


def _kv():
    url = os.getenv("KV_REST_API_URL") or os.getenv("UPSTASH_REDIS_REST_URL")
    token = os.getenv("KV_REST_API_TOKEN") or os.getenv("UPSTASH_REDIS_REST_TOKEN")
    return (url.rstrip("/"), token) if url and token else None


def _kv_get() -> str | None:
    kv = _kv()
    if not kv:
        return None
    try:
        r = requests.post(kv[0], headers={"Authorization": f"Bearer {kv[1]}"},
                          json=["GET", _TOKEN_KEY], timeout=5)
        r.raise_for_status()
        return r.json().get("result")
    except Exception as exc:  # noqa: BLE001 - fall back to env seed
        print(f"Token store read failed: {exc}", file=sys.stderr)
        return None


def _kv_set(value: str) -> None:
    kv = _kv()
    if not kv:
        return
    try:
        r = requests.post(kv[0], headers={"Authorization": f"Bearer {kv[1]}"},
                          json=["SET", _TOKEN_KEY, value], timeout=5)
        r.raise_for_status()
    except Exception as exc:  # noqa: BLE001
        print(f"Token store write failed: {exc}", file=sys.stderr)


def _env_seed() -> tuple[str | None, str]:
    """Decode GARMIN_TOKENS_B64; return (json, fingerprint of the env value)."""
    raw = os.getenv("GARMIN_TOKENS_B64", "").strip()
    fingerprint = hashlib.sha256(raw.encode()).hexdigest()
    if not raw:
        return None, fingerprint
    try:
        data = base64.b64decode(raw).decode()
        json.loads(data)
        return data, fingerprint
    except Exception:  # noqa: BLE001
        print("GARMIN_TOKENS_B64 is not valid base64 JSON.", file=sys.stderr)
        return None, fingerprint


def _seed_token_file() -> str | None:
    """Write the freshest known tokens to /tmp; return the JSON written.

    Stored tokens win only while they descend from the current env seed, so
    pasting a new GARMIN_TOKENS_B64 always takes effect on the next deploy.
    """
    seed, fingerprint = _env_seed()
    data = seed
    stored = _kv_get()
    if stored:
        try:
            record = json.loads(stored)
            if record.get("seed") == fingerprint and record.get("tokens"):
                data = record["tokens"]
        except Exception:  # noqa: BLE001
            pass
    if not data:
        return None
    os.makedirs(_TOKEN_DIR, mode=0o700, exist_ok=True)
    with open(_TOKEN_FILE, "w") as fh:
        fh.write(data)
    os.chmod(_TOKEN_FILE, 0o600)
    return data


os.environ["GARMINTOKENS"] = _TOKEN_DIR
_last_saved = _seed_token_file()
_save_lock = threading.Lock()

import garmin_mcp as core  # noqa: E402

# The package __init__ is imported before this module runs, so its
# import-time token paths still point at ~/. Redirect them to /tmp.
core.tokenstore = _TOKEN_DIR
core.tokenstore_base64 = os.path.join(tempfile.gettempdir(), "garmin-tokens-b64")
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager  # noqa: E402


def _tool_filters():
    enabled, disabled = core._resolve_tool_filters()
    read_only = os.getenv("GARMIN_READ_ONLY", "true").strip().lower() in core._TRUE_VALUES
    return enabled, disabled, read_only and not enabled and not disabled


class _ReadOnlyFilter(core._ToolFilter):
    def _allowed(self, name):
        return name.lower().startswith(("get_", "count_"))


_pending = core._PendingGarminClient(timeout=60.0)
_pending.start(lambda: core.init_api(None, None))
_client = core._GarminProxy(_pending)

_enabled, _disabled, _read_only = _tool_filters()
if _read_only:
    # build_server wraps in _ToolFilter; swap the class for the read-only rule.
    _orig = core._ToolFilter
    core._ToolFilter = _ReadOnlyFilter
    try:
        _fastmcp, _ = core.build_server(_client, set(), set(), stateless_http=True)
    finally:
        core._ToolFilter = _orig
else:
    _fastmcp, _ = core.build_server(_client, _enabled, _disabled, stateless_http=True)


def _persist_refreshed_tokens() -> None:
    """Save tokens back to the store if the library refreshed them."""
    global _last_saved
    if _pending.login_state() != "ready":
        return
    try:
        current = _pending._client.client.dumps()
    except Exception:  # noqa: BLE001
        return
    with _save_lock:
        if current and current != _last_saved:
            _kv_set(json.dumps({"seed": _env_seed()[1], "tokens": current}))
            _last_saved = current


# --------------------------------------------------------------------------
# ASGI app
# --------------------------------------------------------------------------


async def _send_json(send, status, body):
    payload = json.dumps(body).encode()
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-type", b"application/json")]})
    await send({"type": "http.response.body", "body": payload})


async def app(scope, receive, send):
    if scope["type"] == "lifespan":
        while True:
            msg = await receive()
            if msg["type"] == "lifespan.startup":
                await send({"type": "lifespan.startup.complete"})
            elif msg["type"] == "lifespan.shutdown":
                await send({"type": "lifespan.shutdown.complete"})
                return
    if scope["type"] != "http":
        return

    secret = os.getenv("GARMIN_MCP_SECRET", "")
    path = scope.get("path", "").rstrip("/")
    expected = f"/mcp/{secret}"

    if path == "/healthz":
        await _send_json(send, 200, {"ok": True, "login": _pending.login_state()})
        return
    if len(secret) < 24 or not hmac.compare_digest(path.encode(), expected.encode()):
        await _send_json(send, 404, {"error": "not found", "path": path})
        return

    manager = StreamableHTTPSessionManager(
        app=_fastmcp._mcp_server, stateless=True, json_response=True
    )
    async with manager.run():
        await manager.handle_request(scope, receive, send)
    _persist_refreshed_tokens()
