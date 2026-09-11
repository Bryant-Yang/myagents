"""Stdio MCP permission bridge for the myagents Claude Code adapter.

Claude Code spawns this server from ``--mcp-config`` and calls its single
tool whenever a tool use needs an interactive permission decision.  The
server owns no policy of its own: every well-formed request is forwarded
over a private Unix socket to the myagents adapter (which shows the shared
TUI dialog), and every malformed request is denied without contacting the
socket.  The per-process token is the only authentication; it is injected
through the ``--mcp-config`` ``env`` block and never read from disk.
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
import os
from collections.abc import Awaitable, Callable

from mcp.server.lowlevel import Server
from mcp.types import TextContent, Tool


PERMISSION_TOOL_NAME = "request_permission"
_SOCKET_ENV = "MYAGENTS_CLAUDE_PERMISSION_SOCKET"
_TOKEN_ENV = "MYAGENTS_CLAUDE_PERMISSION_TOKEN"
_PROTOCOL_VERSION = 1
_CONNECT_TIMEOUT = 10.0
_REPLY_TIMEOUT = 3600.0
_MAX_PAYLOAD_BYTES = 64 * 1024
_MAX_TOOL_NAME_CHARS = 256


def _deny(message: str) -> list[TextContent]:
    return [TextContent(type="text", text=json.dumps(
        {"behavior": "deny", "message": message},
        ensure_ascii=False,
        separators=(",", ":"),
    ))]


def _reply_content(reply: dict) -> list[TextContent]:
    return [TextContent(type="text", text=json.dumps(
        reply, ensure_ascii=False, separators=(",", ":")))]


async def _forward_over_socket(
    socket_path: str,
    token: str,
    tool_name: str,
    input_value: dict,
) -> dict | None:
    """Return the adapter's validated reply, or ``None`` on any failure."""
    request = json.dumps(
        {
            "version": _PROTOCOL_VERSION,
            "token": token,
            "toolName": tool_name,
            "input": input_value,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8") + b"\n"
    if len(request) > _MAX_PAYLOAD_BYTES:
        return None
    writer: asyncio.StreamWriter | None = None
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_unix_connection(socket_path),
            timeout=_CONNECT_TIMEOUT,
        )
        writer.write(request)
        await writer.drain()
        line = await asyncio.wait_for(
            reader.readline(), timeout=_REPLY_TIMEOUT)
        if not line or len(line) > _MAX_PAYLOAD_BYTES:
            return None
        reply = json.loads(line.decode("utf-8", errors="strict"))
    except (OSError, TimeoutError, UnicodeError, json.JSONDecodeError):
        return None
    finally:
        if writer is not None:
            with contextlib.suppress(Exception):
                await writer.aclose()
    if not isinstance(reply, dict):
        return None
    behavior = reply.get("behavior")
    if behavior == "allow":
        updated = reply.get("updatedInput")
        if not isinstance(updated, dict):
            return None
        return {"behavior": "allow", "updatedInput": updated}
    if behavior == "deny":
        message = reply.get("message")
        if not isinstance(message, str) or not message:
            return None
        return {"behavior": "deny", "message": message}
    return None


async def call_permission_tool(
    name: str,
    arguments: object,
    *,
    socket_path: str,
    token: str,
    _forward: Callable[[str, str, str, dict], Awaitable[dict | None]]
    = _forward_over_socket,
) -> list[TextContent]:
    """Validate one MCP tool call and forward it; malformed input denies."""
    if name != PERMISSION_TOOL_NAME:
        return _deny("myagents 只提供 request_permission 一个工具")
    if not isinstance(arguments, dict):
        return _deny("myagents 拒绝了格式非法的权限请求")
    tool_name = arguments.get("tool_name")
    input_value = arguments.get("input")
    if not isinstance(tool_name, str) or not tool_name:
        return _deny("myagents 拒绝了缺少 tool_name 的权限请求")
    if len(tool_name) > _MAX_TOOL_NAME_CHARS:
        return _deny("myagents 拒绝了超长 tool_name 的权限请求")
    if not isinstance(input_value, dict):
        return _deny("myagents 拒绝了格式非法的权限请求")
    if len(json.dumps(
            input_value, ensure_ascii=False)) > _MAX_PAYLOAD_BYTES:
        return _deny("myagents 拒绝了超过大小上限的权限请求")
    reply = await _forward(socket_path, token, tool_name, input_value)
    if reply is None:
        return _deny("myagents 权限桥不可用，已按默认策略拒绝")
    return _reply_content(reply)


def build_app() -> Server:
    socket_path = os.environ.get(_SOCKET_ENV, "").strip()
    token = os.environ.get(_TOKEN_ENV, "").strip()
    if not socket_path or not token:
        # Without the injected socket credentials the bridge cannot prove it
        # talks to the myagents adapter; failing fast surfaces as
        # mcp_server_errors in system/init and the adapter refuses to start.
        raise SystemExit("permission_server.py 缺少 socket 凭据环境变量")

    app = Server("myagents-claude-permission")

    @app.list_tools()
    async def _list_tools() -> list[Tool]:
        return [Tool(
            name=PERMISSION_TOOL_NAME,
            description=(
                "myagents 权限决策入口：把工具调用转发给用户，"
                "返回 allow 或 deny。"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "tool_name": {"type": "string"},
                    "input": {"type": "object"},
                },
                "required": ["tool_name", "input"],
            },
        )]

    @app.call_tool()
    async def _call_tool(name: str, arguments: object) -> list[TextContent]:
        # 必须经 call_permission_tool 拆包验证：claude 调用本工具时
        # arguments = {tool_name: <被审工具>, input: <被审工具入参>,
        # tool_use_id}，直接转发会把整个 arguments 当成 updatedInput 回写，
        # 导致 allow 被模型侧 schema 校验拒绝。
        return await call_permission_tool(
            name, arguments, socket_path=socket_path, token=token)

    return app


def main() -> None:
    app = build_app()

    from mcp.server.stdio import stdio_server

    async def _run() -> None:
        async with stdio_server() as (read_stream, write_stream):
            await app.run(
                read_stream,
                write_stream,
                app.create_initialization_options(),
            )

    asyncio.run(_run())


if __name__ == "__main__":
    main()
