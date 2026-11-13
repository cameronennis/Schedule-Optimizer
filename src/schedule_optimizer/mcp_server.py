"""The Claude Desktop helper: an MCP server over stdio.

Claude Desktop starts this program and talks to it through stdin/stdout. It uses
the same database file as the app window (WAL mode lets both write), so the
window doesn't need to be open. Tools are defined once in claude_tools.

During development, register it in Claude Desktop's config with `uv run`
(see README), then fully quit and reopen Claude Desktop.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime

import anyio
import mcp_types as types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

from . import claude_tools, db, store

INSTRUCTIONS = claude_tools.INSTRUCTIONS + claude_tools.DESKTOP_INSTRUCTIONS

log = logging.getLogger(__name__)
CTX = claude_tools.Ctx(source="claude-desktop", now=lambda: _now())


def _now() -> datetime:
    with db.session() as s:
        return datetime.now(store.local_tz(store.get_prefs(s)))


def _tool(name: str, spec: dict) -> types.Tool:
    read_only = spec["read_only"]
    return types.Tool(
        name=name,
        description=spec["description"],
        input_schema=spec["input_schema"],
        annotations=types.ToolAnnotations(
            read_only_hint=read_only,
            destructive_hint=False if read_only else name in ("apply_changes",),
            open_world_hint=False,
        ),
    )


def _call(name: str, args: dict) -> dict:
    with db.session() as s:
        return claude_tools.run_tool(s, name, args, CTX)


async def list_tools(ctx, params) -> types.ListToolsResult:
    return types.ListToolsResult(tools=[_tool(n, spec) for n, spec in claude_tools.TOOLS.items()])


async def call_tool(ctx, params: types.CallToolRequestParams) -> types.CallToolResult:
    try:
        result = await anyio.to_thread.run_sync(_call, params.name, params.arguments or {})
    except (ValueError, KeyError) as e:
        msg = e.args[0] if isinstance(e, KeyError) and e.args else str(e)
        return types.CallToolResult(content=[types.TextContent(type="text", text=f"Error: {msg}")], is_error=True)
    except Exception as e:  # noqa: BLE001: report, don't crash the helper
        log.exception("tool %s failed", params.name)
        return types.CallToolResult(content=[types.TextContent(type="text", text=f"Error: {e}")], is_error=True)
    text = json.dumps(result, ensure_ascii=False, indent=1, default=str)
    return types.CallToolResult(content=[types.TextContent(type="text", text=text)])


server = Server("schedule-optimizer", version="0.1.0", instructions=INSTRUCTIONS,
                on_list_tools=list_tools, on_call_tool=call_tool)


async def _serve() -> None:
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


def main() -> None:
    logging.basicConfig(level=logging.WARNING)  # logs go to stderr; stdout carries the protocol
    anyio.run(_serve)


if __name__ == "__main__":
    main()
