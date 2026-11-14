"""The Claude Desktop helper: an MCP server over stdio.

Claude Desktop starts this program and talks to it through stdin/stdout. It uses
the same database file as the app window (WAL mode lets both write), so the
window doesn't need to be open. Tools are defined once in claude_tools.

It's optional, and it works only in chats you activate: "Start a Claude chat"
in the app copies "ACTIVATE SCHEDULE OPTIMIZER FOR THIS CHAT (code …)", and every
call must carry an active code (12 hours, revocable). Without one, every call is
refused, so an installed helper does nothing in other chats.

During development, register it in Claude Desktop's config with `uv run`
(see README), then fully quit and reopen Claude Desktop. Releases install it
through a .mcpb file instead (Phase 9), so users never edit a config file.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

import anyio
import mcp_types as types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

from . import claude_tools, db, store

ACTIVATION = f"""\
IMPORTANT: Use these tools only in a chat where the user has pasted
"{store.ACTIVATION_PHRASE} (code XXX-XXX)". Pass that code as `chat_code` on
every call. Never guess or reuse a code from elsewhere. If the user asks about their
schedule without it, tell them to click "Start a Claude chat" in the Schedule
Optimizer app and paste the message it copies.

"""
INSTRUCTIONS = ACTIVATION + claude_tools.INSTRUCTIONS + claude_tools.DESKTOP_INSTRUCTIONS
CHAT_CODE_FIELD = {
    "type": "string",
    "description": f"The code from \"{store.ACTIVATION_PHRASE} (code …)\" in this chat. Required.",
}

log = logging.getLogger(__name__)
CTX = claude_tools.Ctx(source="claude-desktop", now=lambda: _now())


def _now() -> datetime:
    with db.session() as s:
        return datetime.now(store.local_tz(store.get_prefs(s)))


def _tool(name: str, spec: dict) -> types.Tool:
    read_only = spec["read_only"]
    schema = dict(spec["input_schema"])
    schema["properties"] = {"chat_code": CHAT_CODE_FIELD, **schema.get("properties", {})}
    schema["required"] = ["chat_code", *schema.get("required", [])]
    return types.Tool(
        name=name,
        description=spec["description"] + " Only in a chat activated with a chat code.",
        input_schema=schema,
        annotations=types.ToolAnnotations(
            read_only_hint=read_only,
            destructive_hint=False if read_only else name in ("apply_changes",),
            open_world_hint=False,
        ),
    )


def _seen() -> None:
    """Note that Claude Desktop started the helper, so the app can show "Connected"."""
    with db.session() as s:
        store.update_prefs(s, desktop_helper_seen_at=datetime.now(timezone.utc))


NOT_ACTIVE_MESSAGE = (
    "Schedule Optimizer isn't active in this chat. To use it here, click “Start a Claude chat” in the "
    f"Schedule Optimizer app, then paste the message it copies (“{store.ACTIVATION_PHRASE} (code …)”)."
)


class NotActive(Exception):
    pass


def _call(name: str, args: dict) -> dict:
    args = dict(args)
    code = args.pop("chat_code", None)
    with db.session() as s:
        if not store.chat_code_is_active(s, code):
            raise NotActive(NOT_ACTIVE_MESSAGE)
        return claude_tools.run_tool(s, name, args, CTX)


async def list_tools(ctx, params) -> types.ListToolsResult:
    try:
        await anyio.to_thread.run_sync(_seen)
    except Exception:  # noqa: BLE001: a busy database shouldn't stop the tool list
        log.warning("couldn't record the connection time", exc_info=True)
    return types.ListToolsResult(tools=[_tool(n, spec) for n, spec in claude_tools.TOOLS.items()])


async def call_tool(ctx, params: types.CallToolRequestParams) -> types.CallToolResult:
    try:
        result = await anyio.to_thread.run_sync(_call, params.name, params.arguments or {})
    except NotActive as e:
        return types.CallToolResult(content=[types.TextContent(type="text", text=str(e))], is_error=True)
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
