"""Claude permission bridge (MCP stdio server) contract tests.

The tests exercise the socket-forwarding half of the bridge end to end and
the MCP tool-call validation logic directly.  They never spawn a real MCP
client process.

Run: .venv/bin/python tests/test_claude_permission_server.py
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import sys
import uuid as uuid_module
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from claude_code import permission_server  # noqa: E402
from claude_code.permission_server import (  # noqa: E402
    _MAX_PAYLOAD_BYTES,
    _forward_over_socket,
    call_permission_tool,
)
from mcp.types import TextContent  # noqa: E402


SOCKET_DIR = Path("/tmp/myagents-claude-perm-test")


def _short_socket() -> Path:
    SOCKET_DIR.mkdir(mode=0o700, exist_ok=True)
    return SOCKET_DIR / (uuid_module.uuid4().hex[:8] + ".sock")


def _text(reply: list[TextContent]) -> dict:
    assert len(reply) == 1
    assert reply[0].type == "text"
    return json.loads(reply[0].text)


def run(coro) -> None:
    asyncio.run(asyncio.wait_for(coro, timeout=15.0))


def test_build_app_requires_socket_credentials() -> None:
    saved = {
        key: os.environ.pop(key, None)
        for key in (
            permission_server._SOCKET_ENV,
            permission_server._TOKEN_ENV,
        )
    }
    try:
        try:
            permission_server.build_app()
        except SystemExit:
            pass
        else:
            raise AssertionError("missing credentials must exit at startup")
        os.environ[permission_server._SOCKET_ENV] = "/tmp/x.sock"
        os.environ[permission_server._TOKEN_ENV] = "t"
        app = permission_server.build_app()
        assert app is not None
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    print("ok  缺少 socket 凭据时启动即失败")


def test_malformed_arguments_are_denied_without_socket() -> None:
    async def body() -> None:
        # socket 不存在：任何到达转发层的请求都会 deny，而不是挂起。
        socket_path = str(_short_socket())
        cases = [
            ("wrong_tool", {"tool_name": "Bash", "input": {}}),
            ("request_permission", None),
            ("request_permission", {"tool_name": "", "input": {}}),
            ("request_permission", {"tool_name": "Bash", "input": "x"}),
            ("request_permission", {"tool_name": "x" * 999, "input": {}}),
            (
                "request_permission",
                {"tool_name": "Bash", "input": {"k": "v" * 99999}},
            ),
        ]
        for name, arguments in cases:
            reply = _text(await call_permission_tool(
                name, arguments, socket_path=socket_path, token="t"))
            assert reply["behavior"] == "deny", (name, reply)
        oversized = {
            "tool_name": "Bash",
            "input": {"k": "v" * (_MAX_PAYLOAD_BYTES + 1)},
        }
        reply = _text(await call_permission_tool(
            "request_permission", oversized,
            socket_path=socket_path, token="t"))
        assert reply["behavior"] == "deny"

    run(body())
    print("ok  畸形权限请求直接拒绝且不触碰 socket")


def test_forward_maps_allow_deny_and_garbage() -> None:
    async def body() -> None:
        socket_path = _short_socket()
        received: list[dict] = []

        async def server(reader, writer) -> None:
            line = await reader.readline()
            received.append(json.loads(line))
            script = received[-1].get("toolName")
            if script == "Allow":
                reply = {"behavior": "allow", "updatedInput": {"a": 1}}
            elif script == "Deny":
                reply = {"behavior": "deny", "message": "用户拒绝"}
            elif script == "Garbage":
                writer.write(b"not-json\n")
                await writer.drain()
                writer.close()
                return
            elif script == "Empty":
                writer.close()
                return
            else:
                reply = {"behavior": "explode"}
            writer.write(json.dumps(reply).encode() + b"\n")
            await writer.drain()
            writer.close()
            await writer.wait_closed()

        server = await asyncio.start_unix_server(server, str(socket_path))
        try:
            allowed = await _forward_over_socket(
                str(socket_path), "tok", "Allow", {"a": 1})
            assert allowed == {"behavior": "allow", "updatedInput": {"a": 1}}
            assert received[-1] == {
                "version": 1,
                "token": "tok",
                "toolName": "Allow",
                "input": {"a": 1},
            }
            denied = await _forward_over_socket(
                str(socket_path), "tok", "Deny", {})
            assert denied == {"behavior": "deny", "message": "用户拒绝"}
            garbage = await _forward_over_socket(
                str(socket_path), "tok", "Garbage", {})
            assert garbage is None
            empty = await _forward_over_socket(
                str(socket_path), "tok", "Empty", {})
            assert empty is None
            weird = await _forward_over_socket(
                str(socket_path), "tok", "Weird", {})
            assert weird is None
        finally:
            server.close()
            with contextlib.suppress(BaseException):
                await server.wait_closed()
            with contextlib.suppress(OSError):
                socket_path.unlink()
        missing = await _forward_over_socket(
            str(_short_socket()), "tok", "Allow", {})
        assert missing is None

    run(body())
    print("ok  socket 转发映射 allow/deny 且异常应答归一为拒绝")


def test_tool_call_passes_arguments_through_the_socket() -> None:
    async def body() -> None:
        socket_path = _short_socket()

        async def forward(socket_path, token, tool_name, input_value):
            return await _forward_over_socket(
                socket_path, token, tool_name, input_value)

        async def server(reader, writer) -> None:
            line = await reader.readline()
            request = json.loads(line)
            if request.get("token") != "sekrit":
                reply = {"behavior": "deny", "message": "token 不匹配"}
            else:
                reply = {
                    "behavior": "allow",
                    "updatedInput": request["input"],
                }
            writer.write(json.dumps(reply).encode() + b"\n")
            await writer.drain()
            writer.close()
            await writer.wait_closed()

        server = await asyncio.start_unix_server(server, str(socket_path))
        try:
            allowed = _text(await call_permission_tool(
                "request_permission",
                {"tool_name": "Bash", "input": {"command": "ls"},
                 "permission_suggestions": [{"type": "setMode"}]},
                socket_path=str(socket_path),
                token="sekrit",
                _forward=forward,
            ))
            assert allowed["behavior"] == "allow"
            # 未知字段不进入桥接载荷。
            assert allowed["updatedInput"] == {"command": "ls"}
        finally:
            server.close()
            with contextlib.suppress(BaseException):
                await server.wait_closed()
            with contextlib.suppress(OSError):
                socket_path.unlink()

    run(body())
    print("ok  MCP 工具调用经 socket 全链路回写")


if __name__ == "__main__":
    test_build_app_requires_socket_credentials()
    test_malformed_arguments_are_denied_without_socket()
    test_forward_maps_allow_deny_and_garbage()
    test_tool_call_passes_arguments_through_the_socket()
    print("ok  Claude permission server contract")
