"""
mcp_server.py  —  GraphPulse MCP Server  (JSON-RPC edition)
=============================================================
Single file that covers EVERYTHING MCP-related:

  ┌─────────────────────────────────────────────────────────────────┐
  │  TRANSPORT 1 — Stateless JSON-RPC 2.0   POST /jsonrpc           │
  │      Used by agent_runner.py via dispatch_tool() below          │
  │      Real JSON-RPC protocol, no session handshake needed        │
  ├─────────────────────────────────────────────────────────────────┤
  │  TRANSPORT 2 — MCP SDK StreamableHTTP   POST /mcp               │
  │      Used by Claude Desktop / Cursor (full MCP SDK handshake)   │
  ├─────────────────────────────────────────────────────────────────┤
  │  TRANSPORT 3 — stdio                    python mcp_server.py    │
  │      Used by Claude Desktop (stdio config)   --stdio            │
  ├─────────────────────────────────────────────────────────────────┤
  │  TRANSPORT 4 — REST shim                POST /tools/{name}      │
  │      Kept for debugging / external scripts                      │
  └─────────────────────────────────────────────────────────────────┘

  dispatch_tool(name, input) — importable function for agent_runner.py
      Calls tools DIRECTLY (no HTTP) when imported in the same process.
      Falls back to HTTP /jsonrpc when called from a separate process.

  No mcp_client.py needed — this file is self-contained.

RUN (HTTP mode):   python mcp_server.py
RUN (stdio mode):  python mcp_server.py --stdio
"""

import asyncio
import json
import os
import socket
import sys
import uuid
import threading
import argparse
from typing import Any, Dict, Optional

from dotenv import load_dotenv
load_dotenv(".env")

# ── Neo4j tools (32 functions) ────────────────────────────────────────────────
from neo4j_tools import TOOL_FUNCTIONS, TOOL_SCHEMAS

# ── MCP SDK ───────────────────────────────────────────────────────────────────
try:
    from mcp.server import Server
    from mcp.server.streamable_http import StreamableHTTPServerTransport
    from mcp.server.stdio import stdio_server
    from mcp import types as mcp_types
except ImportError:
    print(
        "[MCP] ERROR: mcp SDK not installed.\n"
        "      Run:  pip install 'mcp[cli]>=1.3'\n"
        "      Then restart this server."
    )
    sys.exit(1)

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route


# ═══════════════════════════════════════════════════════════════════════════════
# 1.  MCP SDK Server object  (used by StreamableHTTP + stdio transports)
# ═══════════════════════════════════════════════════════════════════════════════

mcp = Server("graphpulse-mcp")


@mcp.list_tools()
async def handle_list_tools() -> list[mcp_types.Tool]:
    return [
        mcp_types.Tool(
            name=s["name"],
            description=s.get("description", ""),
            inputSchema=s.get("input_schema", {"type": "object", "properties": {}}),
        )
        for s in TOOL_SCHEMAS
    ]


@mcp.call_tool()
async def handle_call_tool(name: str, arguments: Dict[str, Any]) -> list[mcp_types.TextContent]:
    fn = TOOL_FUNCTIONS.get(name)
    if fn is None:
        return [mcp_types.TextContent(type="text",
                text=json.dumps({"error": f"Unknown tool: {name}"}))]
    try:
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(None, lambda: fn(**arguments))
        return [mcp_types.TextContent(type="text",
                text=result if isinstance(result, str) else json.dumps(result, default=str))]
    except Exception as exc:
        return [mcp_types.TextContent(type="text",
                text=json.dumps({"error": str(exc)}))]


# ═══════════════════════════════════════════════════════════════════════════════
# 2.  dispatch_tool() — importable by agent_runner.py
#
#     Always goes over real HTTP JSON-RPC 2.0 to POST /jsonrpc.
#     mcp_server.py MUST be running as a separate process before app_mcp.py.
#     Port is read from mcp_port.json written at server startup.
#
#     Flow:
#       agent_runner._dispatch_tool()
#         -> dispatch_tool()  [this function]
#           -> HTTP POST /jsonrpc  {"jsonrpc":"2.0","method":"tools/call",...}
#             -> mcp_server._jsonrpc_handler()
#               -> TOOL_FUNCTIONS[name](**arguments)
#                 -> Neo4j query
#               <- JSON result
#             <- {"jsonrpc":"2.0","result":{"content":[{"type":"text","text":"..."}]}}
#           <- result string
#         <- result string
# ═══════════════════════════════════════════════════════════════════════════════

import requests as _requests

class MCPClientError(Exception):
    """Raised when a tool call cannot be completed via MCP JSON-RPC."""


def _get_jsonrpc_url() -> str:
    """Read the live port from mcp_port.json and return the /jsonrpc endpoint URL."""
    try:
        with open("mcp_port.json") as f:
            port = json.load(f)["port"]
        return f"http://127.0.0.1:{port}/jsonrpc"
    except Exception as exc:
        raise MCPClientError(
            f"Cannot read mcp_port.json — is mcp_server.py running? ({exc})"
        )


def dispatch_tool(tool_name: str, tool_input: Dict[str, Any],
                  timeout: float = 30.0) -> str:
    """
    Call a tool via real MCP JSON-RPC 2.0 over HTTP.

    Sends:  POST /jsonrpc
            { "jsonrpc": "2.0", "id": 1, "method": "tools/call",
              "params": { "name": "<tool>", "arguments": { ... } } }

    Returns a JSON string. Raises MCPClientError on any failure.
    Thread-safe — called concurrently from ThreadPoolExecutor in agent_runner.
    """
    url = _get_jsonrpc_url()
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": tool_name,
            "arguments": tool_input,
        },
    }
    try:
        resp = _requests.post(url, json=payload, timeout=timeout)
        resp.raise_for_status()
    except _requests.exceptions.ConnectionError as exc:
        raise MCPClientError(
            f"Cannot reach MCP server at {url} — is mcp_server.py running? ({exc})"
        )
    except _requests.exceptions.Timeout:
        raise MCPClientError(f"MCP server timed out after {timeout}s for tool '{tool_name}'")
    except Exception as exc:
        raise MCPClientError(f"HTTP error calling '{tool_name}': {exc}")

    try:
        data = resp.json()
    except Exception as exc:
        raise MCPClientError(f"Invalid JSON response from MCP server: {exc}")

    if "error" in data:
        raise MCPClientError(f"MCP server error: {data['error'].get('message', data['error'])}")

    try:
        return data["result"]["content"][0]["text"]
    except (KeyError, IndexError, TypeError) as exc:
        raise MCPClientError(f"Unexpected MCP response structure: {exc} | raw: {data}")


def is_server_reachable() -> bool:
    """
    Check if the MCP server is reachable by sending a tools/list JSON-RPC call.
    Returns True if the server responds, False otherwise.
    """
    try:
        url = _get_jsonrpc_url()
        resp = _requests.post(
            url,
            json={"jsonrpc": "2.0", "id": 0, "method": "tools/list", "params": {}},
            timeout=3.0,
        )
        return resp.status_code == 200
    except Exception:
        return False


# ═══════════════════════════════════════════════════════════════════════════════
# 3.  Stateless JSON-RPC 2.0 endpoint   POST /jsonrpc
#
#     Real JSON-RPC 2.0 — no session handshake required.
#     Used by external scripts or a separately-running agent process.
#     Claude Desktop / Cursor should use /mcp (full SDK handshake).
# ═══════════════════════════════════════════════════════════════════════════════

async def _jsonrpc_handler(request: Request):
    """Stateless JSON-RPC 2.0 — handles tools/list and tools/call."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(
            {"jsonrpc": "2.0", "id": None,
             "error": {"code": -32700, "message": "Parse error"}},
            status_code=400)

    rpc_id = body.get("id")
    method = body.get("method", "")
    params = body.get("params", {})

    if method == "initialize":
        return JSONResponse({"jsonrpc": "2.0", "id": rpc_id, "result": {
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "graphpulse-mcp", "version": "1.0"},
        }})

    if method == "notifications/initialized":
        return JSONResponse({"jsonrpc": "2.0", "id": rpc_id, "result": {}})

    if method == "tools/list":
        return JSONResponse({"jsonrpc": "2.0", "id": rpc_id, "result": {
            "tools": [
                {"name": s["name"],
                 "description": s.get("description", ""),
                 "inputSchema": s.get("input_schema", {"type": "object", "properties": {}})}
                for s in TOOL_SCHEMAS
            ]
        }})

    if method == "tools/call":
        name      = params.get("name", "")
        arguments = params.get("arguments", {})
        fn = TOOL_FUNCTIONS.get(name)
        if fn is None:
            return JSONResponse({"jsonrpc": "2.0", "id": rpc_id,
                "error": {"code": -32601, "message": f"Unknown tool: {name}"}})
        try:
            loop   = asyncio.get_running_loop()
            result = await loop.run_in_executor(None, lambda: fn(**arguments))
            text   = result if isinstance(result, str) else json.dumps(result, default=str)
            return JSONResponse({"jsonrpc": "2.0", "id": rpc_id, "result": {
                "content": [{"type": "text", "text": text}],
                "isError": False,
            }})
        except Exception as exc:
            return JSONResponse({"jsonrpc": "2.0", "id": rpc_id, "result": {
                "content": [{"type": "text", "text": json.dumps({"error": str(exc)})}],
                "isError": True,
            }})

    return JSONResponse({"jsonrpc": "2.0", "id": rpc_id,
        "error": {"code": -32601, "message": f"Method not found: {method}"}})


# ═══════════════════════════════════════════════════════════════════════════════
# 4.  MCP SDK StreamableHTTP transport   POST /mcp
#     For Claude Desktop / Cursor — requires full SDK handshake on client side.
# ═══════════════════════════════════════════════════════════════════════════════

async def _mcp_http_handler(request: Request):
    transport = StreamableHTTPServerTransport(mcp_session_id=None)
    async with transport.connect(request.scope, request.receive, request._send):
        await mcp.run(
            transport.read_stream,
            transport.write_stream,
            mcp.create_initialization_options(),
        )


# ═══════════════════════════════════════════════════════════════════════════════
# 5.  REST shim   POST /tools/{name}   GET /tools   GET /
#     Kept for debugging and external scripts.
# ═══════════════════════════════════════════════════════════════════════════════

async def _rest_health(request: Request) -> JSONResponse:
    return JSONResponse({
        "status": "ok",
        "message": "GraphPulse MCP Server",
        "tools": len(TOOL_FUNCTIONS),
        "endpoints": {
            "jsonrpc":  "POST /jsonrpc  — stateless JSON-RPC 2.0 (agent use)",
            "mcp":      "POST /mcp      — MCP SDK StreamableHTTP (Claude Desktop)",
            "rest":     "POST /tools/{name} — REST shim (debugging)",
        }
    })


async def _rest_list_tools(request: Request) -> JSONResponse:
    return JSONResponse({"available_tools": list(TOOL_FUNCTIONS.keys())})


async def _rest_call_tool(request: Request) -> JSONResponse:
    name = request.path_params.get("name", "")
    fn   = TOOL_FUNCTIONS.get(name)
    if fn is None:
        return JSONResponse({"status": "error", "message": f"Unknown tool: {name}"}, status_code=404)
    try:
        body: Dict[str, Any] = {}
        try:
            body = await request.json()
        except Exception:
            pass
        loop   = asyncio.get_running_loop()
        result = await loop.run_in_executor(None, lambda: fn(**body))
        return JSONResponse({"status": "success", "data": result})
    except Exception as exc:
        return JSONResponse({"status": "error", "message": str(exc)}, status_code=500)


# ═══════════════════════════════════════════════════════════════════════════════
# 6.  Starlette app
# ═══════════════════════════════════════════════════════════════════════════════

app = Starlette(routes=[
    Route("/",                  _rest_health),
    Route("/tools",             _rest_list_tools),
    Route("/tools/{name:str}",  _rest_call_tool,   methods=["POST"]),
    Route("/jsonrpc",           _jsonrpc_handler,   methods=["POST"]),
    Route("/mcp",               _mcp_http_handler,  methods=["POST"]),
])


# ═══════════════════════════════════════════════════════════════════════════════
# 7.  stdio transport  (Claude Desktop --stdio mode)
# ═══════════════════════════════════════════════════════════════════════════════

async def _run_stdio():
    async with stdio_server() as (read_stream, write_stream):
        await mcp.run(read_stream, write_stream, mcp.create_initialization_options())


# ═══════════════════════════════════════════════════════════════════════════════
# 8.  Entry point
# ═══════════════════════════════════════════════════════════════════════════════

def _free_port() -> int:
    s = socket.socket(); s.bind(("", 0)); p = s.getsockname()[1]; s.close(); return p


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="GraphPulse MCP Server")
    parser.add_argument("--stdio", action="store_true", help="stdio mode for Claude Desktop")
    parser.add_argument("--port",  type=int, default=None, help="HTTP port (default: auto)")
    args = parser.parse_args()

    if args.stdio:
        print("[MCP] Running in stdio mode", file=sys.stderr)
        asyncio.run(_run_stdio())
    else:
        PORT = args.port or _free_port()
        with open("mcp_port.json", "w") as f:
            json.dump({"port": PORT}, f)

        print(f"\n  GraphPulse MCP Server")
        print(f"  JSON-RPC  -> http://127.0.0.1:{PORT}/jsonrpc     <- agent_runner (direct import)")
        print(f"  MCP SDK   -> http://127.0.0.1:{PORT}/mcp         <- Claude Desktop / Cursor")
        print(f"  REST shim -> http://127.0.0.1:{PORT}/tools/{{name}}  <- debugging")
        print(f"  Tools     -> {len(TOOL_FUNCTIONS)} registered\n")

        uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="warning")