"""Official MCP SDK stdio tests for myagents_mcp.py.

Run: .venv/bin/python tests/test_m3_mcp.py
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from adapters.base import AgentEvent
from control import CommandBus, ControlServer
from orchestrator import Orchestrator
from storage.store import RoomStore

BRIDGE = str(Path(__file__).resolve().parent.parent / "myagents_mcp.py")


class FakeAgent:
    def __init__(self) -> None:
        self.calls = 0

    async def stream(self, prompt: str, workdir: str):
        self.calls += 1
        yield AgentEvent("text", f"mcp-reply-{self.calls}")
        yield AgentEvent("done")


async def open_mcp(workdir: Path, state_root: Path):
    params = StdioServerParameters(
        command=sys.executable,
        args=[
            BRIDGE,
            "--workdir", str(workdir),
            "--state-root", str(state_root),
        ],
        cwd=str(Path(BRIDGE).parent),
    )
    return stdio_client(params)


def test_official_stdio_client() -> None:
    async def run() -> None:
        with tempfile.TemporaryDirectory(dir="/tmp") as tmp:
            root = Path(tmp)
            workdir = root / "work"
            state_root = root / "state"
            workdir.mkdir()
            store = RoomStore(workdir, state_root=state_root)
            orch = Orchestrator(str(workdir), store=store)
            agent = FakeAgent()
            orch.adapters["kimi"] = agent
            bus = CommandBus(orch)
            server = ControlServer(orch, bus)
            bus.start()
            await server.start()
            try:
                params = StdioServerParameters(
                    command=sys.executable,
                    args=[
                        BRIDGE,
                        "--workdir", str(workdir),
                        "--state-root", str(state_root),
                    ],
                    cwd=str(Path(BRIDGE).parent),
                )
                async with stdio_client(params) as (read, write):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        listed = await session.list_tools()
                        tools = {tool.name: tool for tool in listed.tools}
                        assert set(tools) == {
                            "myagents_get_room",
                            "myagents_read_timeline",
                            "myagents_read_events",
                            "myagents_send_message",
                            "myagents_get_command",
                            "myagents_wait_command",
                            "myagents_cancel_command",
                        }
                        read_only = tools["myagents_get_room"].annotations
                        assert read_only.readOnlyHint is True
                        assert read_only.destructiveHint is False
                        assert read_only.idempotentHint is True
                        assert read_only.openWorldHint is False
                        send = tools["myagents_send_message"].annotations
                        assert send.readOnlyHint is False
                        assert send.destructiveHint is True
                        assert send.idempotentHint is False
                        assert send.openWorldHint is True

                        room = await session.call_tool("myagents_get_room", {})
                        assert room.isError is False
                        assert room.structuredContent["room_id"] == store.room_id

                        submitted = await session.call_tool(
                            "myagents_send_message",
                            {"message": "@kimi via MCP", "request_id": "mcp-1"})
                        assert submitted.isError is False
                        command_id = submitted.structuredContent["command_id"]
                        duplicate = await session.call_tool(
                            "myagents_send_message",
                            {"message": "@kimi ignored", "request_id": "mcp-1"})
                        assert duplicate.structuredContent[
                            "command_id"] == command_id

                        waited = await session.call_tool(
                            "myagents_wait_command",
                            {"command_id": command_id, "timeout": 5})
                        assert waited.isError is False
                        assert waited.structuredContent["status"] == "completed"
                        assert agent.calls == 1

                        status = await session.call_tool(
                            "myagents_get_command",
                            {"command_id": command_id})
                        assert status.structuredContent[
                            "status"] == "completed"
                        timeline = await session.call_tool(
                            "myagents_read_timeline",
                            {"after_seq": 0, "limit": 10})
                        records = timeline.structuredContent["items"]
                        assert [item["speaker"] for item in records] == [
                            "user", "kimi"]
                        assert all(item["command_id"] == command_id
                                   for item in records)
                        events = await session.call_tool(
                            "myagents_read_events",
                            {"after_seq": 0, "limit": 50})
                        kinds = [
                            item["kind"]
                            for item in events.structuredContent["items"]
                            if item["command_id"] == command_id]
                        assert kinds[0] == "queued"
                        assert kinds[-1] == "completed"
                        cancelled = await session.call_tool(
                            "myagents_cancel_command",
                            {"command_id": command_id})
                        assert cancelled.structuredContent[
                            "status"] == "completed"

                        # ControlRemoteError → actionable tool error，server 不崩
                        missing = await session.call_tool(
                            "myagents_get_command",
                            {"command_id": "does-not-exist"})
                        assert missing.isError is True
                        assert "NOT_FOUND" in missing.content[0].text
                        # 参数校验失败（timeout 超出 0..30）→ tool error
                        bad_wait = await session.call_tool(
                            "myagents_wait_command",
                            {"command_id": command_id, "timeout": 999})
                        assert bad_wait.isError is True
                        assert "INVALID_PARAMS" in bad_wait.content[0].text
                        # 上述错误之后 server/session 仍可用
                        again = await session.call_tool("myagents_get_room", {})
                        assert again.isError is False
            finally:
                await server.aclose()
                await bus.aclose()
                await orch.aclose()

    asyncio.run(run())
    print("ok  official MCP SDK stdio list_tools/call_tool + control bridge")


def test_unavailable_room_is_tool_error() -> None:
    async def run() -> None:
        with tempfile.TemporaryDirectory(dir="/tmp") as tmp:
            root = Path(tmp)
            workdir = root / "work"
            state_root = root / "absent-state"
            workdir.mkdir()
            params = StdioServerParameters(
                command=sys.executable,
                args=[
                    BRIDGE,
                    "--workdir", str(workdir),
                    "--session", "review",
                    "--state-root", str(state_root),
                ],
                cwd=str(Path(BRIDGE).parent),
            )
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    result = await session.call_tool("myagents_get_room", {})
                    assert result.isError is True
                    text = result.content[0].text
                    assert "main.py" in text and "未发现活跃 TUI" in text
                    assert "--session review" in text
            assert not state_root.exists()

    asyncio.run(run())
    print("ok  TUI 未运行 → actionable MCP tool error 且不创建状态")


def test_bridge_clean_exit_and_stdout_purity() -> None:
    """stdin EOF 后 bridge 必须自行干净退出：rc=0、stdout 零非协议输出。"""
    async def run() -> None:
        with tempfile.TemporaryDirectory(dir="/tmp") as tmp:
            root = Path(tmp)
            workdir = root / "work"
            workdir.mkdir()
            proc = await asyncio.create_subprocess_exec(
                sys.executable, BRIDGE,
                "--workdir", str(workdir),
                "--state-root", str(root / "state"),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(Path(BRIDGE).parent))
            # communicate(input=b"") 关闭 stdin（EOF）；无任何 MCP 请求时
            # stdout 必须完全为空——启动日志/错误只能走 stderr
            out, _ = await asyncio.wait_for(
                proc.communicate(input=b""), timeout=15)
            assert proc.returncode == 0, f"bridge 未干净退出：rc={proc.returncode}"
            assert out == b"", f"stdout 有非协议输出：{out[:200]!r}"

    asyncio.run(run())
    print("ok  bridge stdin EOF 后干净退出（rc=0）且 stdout 零非协议输出")


if __name__ == "__main__":
    test_official_stdio_client()
    test_unavailable_room_is_tool_error()
    test_bridge_clean_exit_and_stdout_purity()
    print("\nM3 MCP stdio 全部通过")
